"""Create the manual-resolution RPCs used by pam-client to resolve
unlinked_no_scheme / unlinked_multi_scheme review rows by hand:

  resolve_review_with_ref(review_id, reference)
      The user pastes the parent ref the parser missed. The ref is stored
      in manual_parent_refs (group()/_set_root_and_state() union it with
      parsed refs, so the link survives every future regroup), then:
        - parent already in `applications` and grouped -> link immediately
          (membership move, linked_by='manual', review resolved);
        - parent not stored (or stored but not grouped yet) -> catalogue in
          missing_parents for the next parent hunt and resolve the review
          row as 'queued_for_hunt' (when the hunt lands the parent,
          mark_found() regroups the child and the manual ref links it);
        - the ref was already hunted and definitively missed -> 'exhausted'.
  resolve_review_singleton(review_id)
      The user declares the application a standalone scheme. Sets
      role/role_override='original' (group() never re-derives an override
      and never relocates an overridden uid), moves it to its own project
      when it shares a provisional bucket, and resolves the review row.

Run once (idempotent). Worker unaffected except through regroup_pending.

  python scripts/apply_manual_resolution_rpc.py          # dry-run (rolls back)
  python scripts/apply_manual_resolution_rpc.py --apply  # create/replace

Security: same model as apply_grouping_resolution — SECURITY DEFINER,
locked to authenticated callers, validates the review row is pending, and
only performs the specific grouping moves. manual_parent_refs is
read-only for authenticated users (so the UI can show "manually linked as
<ref>"); writes happen only here (definer) and via cascade deletes.

UI contract: docs/2026-10-12-manual-resolution-ui-spec.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from pam.config import load_dotenv

SQL = r"""
-- User-supplied parent refs. Also created by pam.projects.ensure_schema()
-- (the worker unions these into every ref derivation); the CREATE here
-- keeps this script standalone-applicable. Definition must match SCHEMA.
CREATE TABLE IF NOT EXISTS manual_parent_refs (
    uid         TEXT PRIMARY KEY REFERENCES applications(uid) ON DELETE CASCADE,
    authority   TEXT NOT NULL,
    reference   TEXT NOT NULL,          -- user-supplied parent ref (upper-case)
    created_by  UUID,                   -- auth.uid() of the resolving user
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE manual_parent_refs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON manual_parent_refs FROM anon;
GRANT SELECT ON manual_parent_refs TO authenticated;
DROP POLICY IF EXISTS "authenticated read" ON manual_parent_refs;
CREATE POLICY "authenticated read" ON manual_parent_refs
    FOR SELECT TO authenticated USING (true);

-- role_override backs resolve_review_singleton (also in projects.MIGRATIONS)
ALTER TABLE project_applications ADD COLUMN IF NOT EXISTS role_override TEXT;

CREATE OR REPLACE FUNCTION public.resolve_review_with_ref(
    p_review_id bigint, p_reference text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  r grouping_review%ROWTYPE;
  v_ref text;
  v_year smallint;
  v_m text[];
  v_parent_uid text;
  v_pid bigint;
  v_mp_status text;
BEGIN
  IF auth.uid() IS NULL THEN
    RAISE EXCEPTION 'not authenticated';
  END IF;

  SELECT * INTO r FROM grouping_review WHERE id = p_review_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'review row % not found', p_review_id;
  END IF;
  IF r.status <> 'pending' THEN
    RAISE EXCEPTION 'review row % is not pending (status=%)', p_review_id, r.status;
  END IF;
  IF r.reason NOT IN ('unlinked_no_scheme', 'unlinked_multi_scheme')
     OR r.uid IS NULL THEN
    RAISE EXCEPTION 'review row % is not an unlinked-application row (reason=%)',
      p_review_id, r.reason;
  END IF;

  -- normalise: trim, collapse whitespace, drop spaces around '/',
  -- upper-case (missing_parents.reference and the hunt lookups are upper)
  v_ref := upper(regexp_replace(
             regexp_replace(btrim(coalesce(p_reference, '')),
                            '\s+', ' ', 'g'),
             '\s*/\s*', '/', 'g'));
  IF v_ref !~ '^[A-Z0-9][A-Z0-9./-]{2,49}$' OR v_ref !~ '\d' THEN
    RAISE EXCEPTION 'not a plausible council reference: %', p_reference;
  END IF;

  -- remember the user-supplied ref: group()/_set_root_and_state() union it
  -- with parsed refs from now on, so the link survives regroups
  INSERT INTO manual_parent_refs (uid, authority, reference, created_by)
  VALUES (r.uid, r.authority, v_ref, auth.uid())
  ON CONFLICT (uid) DO UPDATE SET reference = EXCLUDED.reference,
      created_by = EXCLUDED.created_by, created_at = now();

  -- already in our data? (same local-first lookup the parent hunt uses)
  SELECT a.uid INTO v_parent_uid
  FROM applications a
  WHERE a.authority = r.authority
    AND (upper(a.reference) = v_ref
         OR starts_with(upper(a.reference), v_ref || '/'))
  ORDER BY a.start_date ASC NULLS LAST
  LIMIT 1;

  IF v_parent_uid IS NOT NULL THEN
    SELECT pa.project_id INTO v_pid
    FROM project_applications pa WHERE pa.uid = v_parent_uid LIMIT 1;
  END IF;

  IF v_pid IS NOT NULL THEN
    -- ---- link immediately ----
    -- a uid belongs to exactly one project (unique index
    -- project_applications_uid_key): move it out of any other project
    -- first, queueing abandoned projects for re-derivation/retirement
    INSERT INTO regroup_pending (project_id)
    SELECT project_id FROM project_applications WHERE uid = r.uid
    ON CONFLICT (project_id) DO NOTHING;
    DELETE FROM project_applications WHERE uid = r.uid AND project_id <> v_pid;
    INSERT INTO project_applications
      (project_id, uid, parent_ref, is_root, role, has_parent_refs, linked_by)
    SELECT v_pid, r.uid, v_ref, false,
           COALESCE((SELECT role FROM project_applications
                     WHERE uid = r.uid LIMIT 1), 'original'),
           true, 'manual'
    ON CONFLICT (project_id, uid) DO UPDATE
      SET parent_ref = EXCLUDED.parent_ref, linked_by = 'manual';
    -- refresh aggregates for the target scheme (set-based, same as
    -- apply_grouping_resolution's assign path)
    UPDATE projects p SET
      n_applications = s.n, latest_uid = s.latest_uid, name = s.name,
      first_seen = s.first_seen, last_updated = s.last_updated
    FROM (
      SELECT pa.project_id, count(*) AS n,
        (array_agg(a.uid ORDER BY coalesce(a.decided_date, a.last_updated)
                   DESC NULLS LAST, a.uid))[1] AS latest_uid,
        (array_agg(a.address ORDER BY length(a.address))
           FILTER (WHERE nullif(btrim(a.address), '') IS NOT NULL))[1] AS name,
        min(a.first_seen) AS first_seen, max(a.last_updated) AS last_updated
      FROM project_applications pa JOIN applications a ON a.uid = pa.uid
      WHERE pa.project_id = v_pid GROUP BY pa.project_id
    ) s WHERE p.id = s.project_id;
    INSERT INTO regroup_pending (project_id) VALUES (v_pid)
      ON CONFLICT (project_id) DO NOTHING;
    UPDATE grouping_review SET status='resolved', resolution=v_pid::text,
      resolved_by=auth.uid(), resolved_at=now() WHERE id = p_review_id;
    RETURN jsonb_build_object('action', 'linked', 'uid', r.uid,
                              'reference', v_ref, 'parent_uid', v_parent_uid,
                              'project_id', v_pid);
  END IF;

  -- ---- not resolvable yet: catalogue for the parent hunt ----
  -- (also the path for a parent that is stored but not grouped yet — the
  -- hunt's local-first lookup resolves it without an API call)
  v_m := regexp_match(v_ref, '(?:19|20)\d{2}');
  IF v_m IS NOT NULL AND v_m[1]::int
       BETWEEN 2000 AND EXTRACT(YEAR FROM now())::int + 1 THEN
    v_year := v_m[1]::int;
  ELSE
    v_m := regexp_match(v_ref, '(?:^|/)(\d{2})/');
    IF v_m IS NOT NULL THEN
      v_year := CASE WHEN v_m[1]::int < 30 THEN 2000 + v_m[1]::int
                     ELSE 1900 + v_m[1]::int END;
      IF v_year NOT BETWEEN 2000 AND EXTRACT(YEAR FROM now())::int + 1 THEN
        v_year := NULL;
      END IF;
    END IF;
  END IF;

  INSERT INTO missing_parents (authority, reference, ref_year,
                               requested_by, project_id)
  VALUES (r.authority, v_ref, v_year, r.uid,
          (SELECT project_id FROM project_applications
           WHERE uid = r.uid LIMIT 1))
  ON CONFLICT (authority, reference) DO NOTHING;

  SELECT status INTO v_mp_status FROM missing_parents
  WHERE authority = r.authority AND reference = v_ref;

  IF v_mp_status = 'exhausted' THEN
    -- definitive miss from a previous hunt: PlanIt does not hold this ref.
    -- The review row stays pending so the user can pick another action.
    RETURN jsonb_build_object('action', 'exhausted', 'uid', r.uid,
                              'reference', v_ref);
  END IF;

  -- ref catalogued; the row leaves the manual queue and becomes a normal
  -- provisional/missing-parents case (same lifecycle as a parser-found ref)
  UPDATE grouping_review SET status='resolved', resolution='queued_for_hunt',
    resolved_by=auth.uid(), resolved_at=now() WHERE id = p_review_id;
  RETURN jsonb_build_object('action', 'queued_for_hunt', 'uid', r.uid,
                            'reference', v_ref, 'ref_year', v_year);
END;
$$;

CREATE OR REPLACE FUNCTION public.resolve_review_singleton(
    p_review_id bigint)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  r grouping_review%ROWTYPE;
  v_pid bigint;
  v_new_pid bigint;
BEGIN
  IF auth.uid() IS NULL THEN
    RAISE EXCEPTION 'not authenticated';
  END IF;

  SELECT * INTO r FROM grouping_review WHERE id = p_review_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'review row % not found', p_review_id;
  END IF;
  IF r.status <> 'pending' THEN
    RAISE EXCEPTION 'review row % is not pending (status=%)', p_review_id, r.status;
  END IF;
  IF r.reason NOT IN ('unlinked_no_scheme', 'unlinked_multi_scheme')
     OR r.uid IS NULL THEN
    RAISE EXCEPTION 'review row % is not an unlinked-application row (reason=%)',
      p_review_id, r.reason;
  END IF;

  SELECT pa.project_id INTO v_pid FROM project_applications pa
  WHERE pa.uid = r.uid LIMIT 1;
  IF v_pid IS NULL THEN
    RAISE EXCEPTION
      'application % is not grouped yet; try again after the next sync', r.uid;
  END IF;

  -- sharing a provisional bucket with other no-ref records? move it out
  -- into its own project at the same address (same-address siblings are
  -- allowed; the alias keeps pointing at the bucket as the address seed)
  IF (SELECT count(*) FROM project_applications
      WHERE project_id = v_pid) > 1 THEN
    INSERT INTO projects (authority, address_key, grouping_state)
    SELECT p.authority, p.address_key, 'provisional'
    FROM projects p WHERE p.id = v_pid
    RETURNING id INTO v_new_pid;
    UPDATE project_applications SET project_id = v_new_pid
    WHERE project_id = v_pid AND uid = r.uid;
    INSERT INTO regroup_pending (project_id) VALUES (v_pid)
      ON CONFLICT (project_id) DO NOTHING;
    v_pid := v_new_pid;
  END IF;

  -- pin the role: group() uses role_override verbatim and never relocates
  -- an overridden uid into an address bucket (references can still move it)
  UPDATE project_applications
  SET role = 'original', role_override = 'original', linked_by = 'manual'
  WHERE uid = r.uid;

  INSERT INTO regroup_pending (project_id) VALUES (v_pid)
    ON CONFLICT (project_id) DO NOTHING;
  UPDATE grouping_review SET status='resolved', resolution='singleton',
    resolved_by=auth.uid(), resolved_at=now() WHERE id = p_review_id;
  RETURN jsonb_build_object('action', 'singleton', 'uid', r.uid,
                            'project_id', v_pid);
END;
$$;

REVOKE ALL ON FUNCTION public.resolve_review_with_ref(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.resolve_review_with_ref(bigint, text)
  TO authenticated;
REVOKE ALL ON FUNCTION public.resolve_review_singleton(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.resolve_review_singleton(bigint)
  TO authenticated;
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not configured")
    conn = psycopg.connect(url, options="-c statement_timeout=120000")
    try:
        with conn.cursor() as cur:
            cur.execute(SQL)
        if args.apply:
            conn.commit()
            print("APPLIED resolve_review_with_ref + resolve_review_singleton")
        else:
            conn.rollback()
            print("DRY RUN — functions created then rolled back (syntax OK)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
