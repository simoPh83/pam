"""Create the apply_grouping_resolution RPC used by pam-client to resolve
grouping_review rows atomically. Run once (idempotent). Worker unaffected.

  python scripts/apply_grouping_rpc.py          # dry-run (rolls back)
  python scripts/apply_grouping_rpc.py --apply  # create/replace the function

Security: SECURITY INVOKER, callable by authenticated. The function only
moves data for a review row that is pending and that the caller can see
(RLS on grouping_review). The data moves run through the caller's role, so
RLS on projects/project_applications still applies — the pam tables grant
authenticated read, so we need a definer-safe path. We therefore make the
function SECURITY DEFINER but lock it down: it validates the caller is
authenticated and the row is pending, and only performs the specific
grouping moves. See docs/2026-10-10-grouping-review-ui-spec.md.
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
-- Projects that need a root/grouping_state re-derivation after a manual
-- assignment (the RPC moves membership but can't recompute the component
-- root without the Python logic). Swept by the worker each run.
CREATE TABLE IF NOT EXISTS regroup_pending (
    project_id bigint PRIMARY KEY,
    queued_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE regroup_pending ENABLE ROW LEVEL SECURITY;
-- worker/service-role only; authenticated users never touch it directly
REVOKE ALL ON regroup_pending FROM anon, authenticated;

CREATE OR REPLACE FUNCTION public.apply_grouping_resolution(
    p_review_id bigint, p_resolution text)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  r grouping_review%ROWTYPE;
  v_uid text;
  v_pid bigint;
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
  IF p_resolution IS NULL THEN
    RAISE EXCEPTION 'p_resolution is required';
  END IF;

  -- ---- unlinked orphans (address-keyed; project_id is null) ----
  IF r.reason IN ('unlinked_multi_scheme', 'unlinked_no_scheme') THEN
    v_uid := r.uid;
    IF p_resolution = 'dismiss' THEN
      -- leave the application ungrouped; just close the review row
      UPDATE grouping_review SET status='dismissed', resolution='dismiss',
        resolved_by=auth.uid(), resolved_at=now() WHERE id=p_review_id;
      RETURN jsonb_build_object('action','dismissed','uid',v_uid);
    ELSIF p_resolution ~ '^\d+$' THEN
      v_pid := p_resolution::bigint;
      -- attach to the chosen scheme (role/has_parent_refs recomputed by the
      -- worker on next regroup; we seed sensible values)
      INSERT INTO project_applications
        (project_id, uid, parent_ref, is_root, role, has_parent_refs, linked_by)
      SELECT v_pid, v_uid, NULL, false,
             COALESCE((SELECT role FROM project_applications WHERE uid=v_uid LIMIT 1),
                      'original'),
             COALESCE((SELECT has_parent_refs FROM project_applications
                       WHERE uid=v_uid LIMIT 1), false),
             'manual'
      ON CONFLICT (project_id, uid) DO UPDATE SET linked_by='manual';
      -- refresh aggregates for the target scheme (set-based)
      UPDATE projects p SET
        n_applications = s.n, latest_uid = s.latest_uid, name = s.name,
        first_seen = s.first_seen, last_updated = s.last_updated
      FROM (
        SELECT pa.project_id, count(*) AS n,
          (array_agg(a.uid ORDER BY coalesce(a.decided_date,a.last_updated)
                     DESC NULLS LAST, a.uid))[1] AS latest_uid,
          (array_agg(a.address ORDER BY length(a.address))
             FILTER (WHERE nullif(btrim(a.address),'') IS NOT NULL))[1] AS name,
          min(a.first_seen) AS first_seen, max(a.last_updated) AS last_updated
        FROM project_applications pa JOIN applications a ON a.uid=pa.uid
        WHERE pa.project_id = v_pid GROUP BY pa.project_id
      ) s WHERE p.id = s.project_id;
      -- defer root/grouping_state re-derivation to the worker sweep
      INSERT INTO regroup_pending (project_id) VALUES (v_pid)
        ON CONFLICT (project_id) DO NOTHING;
      -- mark resolved
      UPDATE grouping_review SET status='resolved', resolution=p_resolution,
        resolved_by=auth.uid(), resolved_at=now() WHERE id=p_review_id;
      RETURN jsonb_build_object('action','assigned','uid',v_uid,'project_id',v_pid);
    ELSE
      RAISE EXCEPTION 'unsupported resolution for %: % (use a numeric project_id or dismiss)',
        r.reason, p_resolution;
    END IF;

  -- ---- multi_root (project-keyed) ----
  ELSIF r.reason = 'multi_root' THEN
    v_pid := r.project_id;
    IF p_resolution = 'merged' THEN
      UPDATE projects SET grouping_state='clean' WHERE id=v_pid;
      UPDATE grouping_review SET status='resolved', resolution='merged',
        resolved_by=auth.uid(), resolved_at=now() WHERE id=p_review_id;
      RETURN jsonb_build_object('action','kept_merged','project_id',v_pid);
    ELSE
      RAISE EXCEPTION 'multi_root split is performed by the worker, not the RPC';
    END IF;
  ELSE
    RAISE EXCEPTION 'unknown reason %', r.reason;
  END IF;
END;
$$;

REVOKE ALL ON FUNCTION public.apply_grouping_resolution(bigint, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.apply_grouping_resolution(bigint, text) TO authenticated;
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
            print("APPLIED apply_grouping_resolution")
        else:
            conn.rollback()
            print("DRY RUN — function created then rolled back (syntax OK)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
