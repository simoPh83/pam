"""Rebuild project grouping around reference-connected scheme families.

Dry-run by default: all work happens in one transaction that is rolled back,
and validation counts are printed. Use --apply to commit (stop the worker
first). See docs/2026-10-10-scheme-grouping-spec.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from pam.config import load_dotenv
from pam.projects import address_key, parent_refs_of, role_of

log = logging.getLogger("regroup_schemes")

DDL = """
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_authority_address_key_key;
ALTER TABLE projects ALTER COLUMN address_key DROP NOT NULL;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS grouping_state TEXT NOT NULL DEFAULT 'clean';
ALTER TABLE project_applications ADD COLUMN IF NOT EXISTS linked_by TEXT NOT NULL DEFAULT 'reference';
CREATE TABLE IF NOT EXISTS grouping_review (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    authority   TEXT NOT NULL,
    project_id  BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    uid         TEXT,
    reason      TEXT NOT NULL,
    candidates  JSONB NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    resolution  TEXT,
    resolved_by UUID,
    resolved_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE grouping_review ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON grouping_review FROM anon;
GRANT SELECT ON grouping_review TO authenticated;
GRANT UPDATE (status, resolution, resolved_by, resolved_at)
    ON grouping_review TO authenticated;
DROP POLICY IF EXISTS "authenticated read" ON grouping_review;
CREATE POLICY "authenticated read" ON grouping_review
    FOR SELECT TO authenticated USING (true);
DROP POLICY IF EXISTS "authenticated resolve" ON grouping_review;
CREATE POLICY "authenticated resolve" ON grouping_review
    FOR UPDATE TO authenticated USING (true)
    WITH CHECK (status IN ('resolved', 'dismissed'));
"""


# Tables keyed by projects(id) whose rows are re-pointed when a project id is
# retired. (table, unique-key column besides project_id, or None when
# project_id alone is the key). Verified against information_schema.
DEPENDENT_TABLES = (
    ("missing_parents", None),
    ("ui_project_stars", "user_id"),
    ("ui_project_meta", "user_id"),
    ("ui_project_practices", "practice_id"),
    ("ui_project_agent_presence", None),
    ("ui_project_root_tsv", None),
)


class Dsu:
    def __init__(self, ids):
        self.parent = {i: i for i in ids}

    def find(self, x):
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def load(conn):
    apps = {}
    rows = conn.execute(
        "SELECT uid, authority, reference, address, postcode, description, "
        "app_type, start_date FROM applications").fetchall()
    for uid, auth, ref, addr, pc, desc, app_type, start in rows:
        apps[uid] = {
            "uid": uid, "authority": auth, "reference": (ref or "").upper(),
            "address": addr, "postcode": pc, "description": desc or "",
            "app_type": app_type, "start_date": start,
            "refs": parent_refs_of(desc, auth),
            "role": role_of(desc, app_type, auth),
        }
    # authority -> {lookup key -> (start_date, uid)}. A stored reference is
    # indexed under itself and every prefix obtained by stripping trailing
    # "/SEGMENT" parts, so a cited ref matches either the exact stored ref or
    # a stored ref carrying a type suffix (same semantics as _resolve_refs).
    ref_index = defaultdict(dict)
    for a in apps.values():
        if not a["reference"]:
            continue
        ref, start, uid = a["reference"], a["start_date"], a["uid"]
        key = ref
        while key:
            prev = ref_index[a["authority"]].get(key)
            if prev is None or (start is not None and (prev[0] is None
                               or start < prev[0])):
                ref_index[a["authority"]][key] = (start, uid)
            if "/" not in key:
                break
            key = key.rsplit("/", 1)[0]
    old_projects = {}
    for pid, auth, key, root in conn.execute(
            "SELECT id, authority, address_key, root_uid FROM projects"):
        old_projects[pid] = {"authority": auth, "address_key": key,
                             "root_uid": root, "members": set()}
    for pid, uid in conn.execute(
            "SELECT project_id, uid FROM project_applications"):
        if pid in old_projects:
            old_projects[pid]["members"].add(uid)
    return apps, ref_index, old_projects


def resolve(ref_index, authority, ref):
    """Uid whose stored reference matches the cited ref (earliest start_date)."""
    hit = ref_index.get(authority, {}).get(ref.upper())
    return hit[1] if hit else None


def build_components(apps, ref_index):
    dsu = Dsu(apps.keys())
    resolved_out = defaultdict(set)     # uid -> uids it cites (resolved)
    unresolved_refs = defaultdict(list)  # uid -> refs that don't resolve
    for a in apps.values():
        for ref in a["refs"]:
            target = resolve(ref_index, a["authority"], ref)
            if target and target != a["uid"]:
                dsu.union(a["uid"], target)
                resolved_out[a["uid"]].add(target)
            elif target != a["uid"]:
                unresolved_refs[a["uid"]].append(ref)
    comps = defaultdict(list)
    for uid in apps:
        comps[dsu.find(uid)].append(uid)
    return list(comps.values()), resolved_out, unresolved_refs


def classify(comp, apps, resolved_out, unresolved_refs):
    """Return (root_uid, grouping_state). Roots have no resolved parent."""
    roots = [u for u in comp if u not in resolved_out]
    if not roots or len(roots) > 1:
        # >1: several candidate origins kept merged pending review.
        # 0:  reference cycle — pick earliest, flag for review.
        state = "ambiguous"
        roots = roots or list(comp)
    else:
        root = roots[0]
        state = ("clean" if apps[root]["role"] == "original"
                 and not unresolved_refs.get(root) else "provisional")
    roots.sort(key=lambda u: (apps[u]["start_date"] is None,
                              apps[u]["start_date"], u))
    return roots[0], state


def candidate_info(apps, uid):
    a = apps[uid]
    return {"uid": uid, "description": a["description"][:200],
            "app_type": a["app_type"],
            "start_date": str(a["start_date"]) if a["start_date"] else None}


def repoint(cur):
    """Re-point dependents of retired project ids using the _repoint temp
    table (old_id, new_id). Set-based; dedupes on each table's unique key."""
    for table, key in DEPENDENT_TABLES:
        if key:
            cur.execute(
                f"DELETE FROM {table} t USING _repoint r "
                f"WHERE t.project_id = r.old_id AND EXISTS ("
                f"  SELECT 1 FROM {table} x "
                f"  WHERE x.project_id = r.new_id AND x.{key} = t.{key})")
        else:
            cur.execute(
                f"DELETE FROM {table} t USING _repoint r "
                f"WHERE t.project_id = r.old_id AND EXISTS ("
                f"  SELECT 1 FROM {table} x WHERE x.project_id = r.new_id)")
        cur.execute(
            f"UPDATE {table} t SET project_id = r.new_id "
            f"FROM _repoint r WHERE t.project_id = r.old_id")
    # project_aliases: unique on (authority, address_key)
    cur.execute(
        "DELETE FROM project_aliases a USING _repoint r "
        "WHERE a.project_id = r.old_id AND EXISTS ("
        "  SELECT 1 FROM project_aliases x "
        "  WHERE x.project_id = r.new_id AND x.authority = a.authority "
        "  AND x.address_key = a.address_key)")
    cur.execute(
        "UPDATE project_aliases a SET project_id = r.new_id "
        "FROM _repoint r WHERE a.project_id = r.old_id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Commit changes (default: dry-run, rolled back)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not configured")

    conn = psycopg.connect(url, options="-c statement_timeout=600000")
    stats = Counter()

    def maybe_commit(phase):
        # --apply commits per phase so WAL can recycle between checkpoints;
        # dry-run stays one transaction and rolls back at the end.
        if args.apply:
            conn.commit()
            log.info("Phase committed: %s", phase)

    try:
        with conn.cursor() as cur:
            for stmt in DDL.split(";"):
                if stmt.strip():
                    cur.execute(stmt)
        maybe_commit("ddl")
        apps, ref_index, old_projects = load(conn)
        log.info("Loaded %s applications, %s old projects",
                 len(apps), len(old_projects))

        comps, resolved_out, unresolved_refs = build_components(apps, ref_index)
        schemes = []  # {members, root_uid, state}
        for comp in comps:
            root_uid, state = classify(comp, apps, resolved_out, unresolved_refs)
            schemes.append({"members": list(comp), "root_uid": root_uid,
                            "state": state})
            stats[f"component_{state}"] += 1

        # Address -> schemes map, built BEFORE inferred attachments so the
        # number of schemes at an address is stable during the heuristic.
        addr_schemes = defaultdict(set)  # (authority, key) -> scheme objects
        for s in schemes:
            for uid in s["members"]:
                key = address_key(apps[uid]["address"], apps[uid]["postcode"])
                if key:
                    addr_schemes[(apps[uid]["authority"], key)].add(id(s))
        scheme_by_obj = {id(s): s for s in schemes}

        # No-ref non-original singletons: auto-attach or queue for review
        review_rows = []  # (authority, scheme, uid, reason, candidates)
        for s in schemes:
            if len(s["members"]) != 1:
                continue
            uid = s["members"][0]
            a = apps[uid]
            if a["role"] == "original" or a["refs"]:
                continue
            key = address_key(a["address"], a["postcode"])
            others = (addr_schemes.get((a["authority"], key), set())
                      - {id(s)})
            if len(others) == 1:
                target = scheme_by_obj[next(iter(others))]
                target["members"].append(uid)
                s["members"] = []
                a["_linked_by"] = "inferred"
                stats["inferred_attachments"] += 1
            else:
                reason = ("unlinked_multi_scheme" if others
                          else "unlinked_no_scheme")
                cands = [candidate_info(apps, scheme_by_obj[o]["root_uid"])
                         for o in sorted(others)]
                review_rows.append((a["authority"], s, uid, reason, cands))
                stats[f"review_{reason}"] += 1
        schemes = [s for s in schemes if s["members"]]

        # Ambiguous components: one review row each, candidates = roots
        for s in schemes:
            if s["state"] != "ambiguous":
                continue
            roots = sorted(u for u in s["members"] if u not in resolved_out)
            review_rows.append((apps[s["root_uid"]]["authority"], s, None,
                                "multi_root",
                                [candidate_info(apps, u) for u in roots]))
            stats["review_multi_root"] += 1

        # --- map old project ids -------------------------------------------
        uid_old_pid = {}
        for pid, p in old_projects.items():
            for uid in p["members"]:
                uid_old_pid[uid] = pid

        # A scheme inherits the id of the old project whose root it contains.
        scheme_old_id = {}
        taken = set()
        for s in sorted(schemes,
                        key=lambda s: -(len(s["members"]))):
            cand = {uid_old_pid[u] for u in s["members"] if u in uid_old_pid}
            cand -= taken
            if not cand:
                continue
            pref = [pid for pid in cand
                    if old_projects[pid]["root_uid"] in s["members"]]
            pick = min(pref) if pref else min(cand)
            scheme_old_id[id(s)] = pick
            taken.add(pick)
        retired = [pid for pid in old_projects if pid not in taken]
        stats["old_projects_kept"] = len(taken)
        stats["old_projects_retired"] = len(retired)
        stats["schemes_new_ids"] = len(schemes) - len(scheme_old_id)

        # --- write (set-based; DDL above holds ACCESS EXCLUSIVE on projects,
        # so keep this phase fast) ------------------------------------------
        with conn.cursor() as cur:
            # Assign ids: reuse inherited old ids; allocate fresh ones with a
            # single sequence read.
            n_new = len(schemes) - len(scheme_old_id)
            cur.execute(
                "SELECT nextval(pg_get_serial_sequence('projects','id')) "
                "FROM generate_series(1, %s)", (max(n_new, 0),))
            fresh = [r[0] for r in cur.fetchall()]
            fi = iter(fresh)
            for s in schemes:
                s["project_id"] = scheme_old_id.get(id(s)) or next(fi)

            cur.execute("""
                CREATE TEMP TABLE _schemes (
                    id bigint, authority text, address_key text,
                    root_uid text, grouping_state text, is_new boolean
                ) ON COMMIT DROP""")
            with cur.copy("COPY _schemes FROM STDIN") as cp:
                for s in schemes:
                    root = apps[s["root_uid"]]
                    cp.write_row((
                        s["project_id"], root["authority"],
                        address_key(root["address"], root["postcode"]),
                        s["root_uid"], s["state"],
                        id(s) not in scheme_old_id))
            cur.execute("""
                INSERT INTO projects (id, authority, address_key, root_uid,
                                      grouping_state)
                    OVERRIDING SYSTEM VALUE
                SELECT id, authority, address_key, root_uid, grouping_state
                FROM _schemes WHERE is_new""")
            cur.execute("""
                UPDATE projects p SET authority=s.authority,
                    address_key=s.address_key, root_uid=s.root_uid,
                    grouping_state=s.grouping_state
                FROM _schemes s WHERE p.id=s.id AND NOT s.is_new""")

            # Re-point dependents of retired ids to the scheme that now
            # contains the old project's root, then delete the retired rows.
            uid_scheme = {uid: s for s in schemes for uid in s["members"]}
            cur.execute("CREATE TEMP TABLE _repoint (old_id bigint, "
                        "new_id bigint) ON COMMIT DROP")
            with cur.copy("COPY _repoint FROM STDIN") as cp:
                repointed = 0
                orphans = []
                for pid in retired:
                    target = uid_scheme.get(old_projects[pid]["root_uid"])
                    if target is None:
                        orphans.append(pid)
                    else:
                        cp.write_row((pid, target["project_id"]))
                        repointed += 1
            stats["old_ids_repointed"] = repointed
            stats["old_ids_orphaned"] = len(orphans)
            repoint(cur)
            cur.execute("DELETE FROM projects p USING _repoint r "
                        "WHERE p.id = r.old_id")
            if orphans:
                cur.execute("DELETE FROM projects WHERE id = ANY(%s)",
                            (orphans,))
        maybe_commit("projects+repoint")

        with conn.cursor() as cur:
            log.info("Rebuilding %s memberships",
                     sum(len(s["members"]) for s in schemes))
            cur.execute("""
                CREATE TEMP TABLE _members (
                    project_id bigint, uid text, parent_ref text,
                    is_root boolean, role text, has_parent_refs boolean,
                    linked_by text) ON COMMIT DROP""")
            with cur.copy("COPY _members FROM STDIN") as cp:
                for s in schemes:
                    for uid in s["members"]:
                        a = apps[uid]
                        targets = sorted(
                            resolved_out.get(uid) or (),
                            key=lambda t: (apps[t]["start_date"] is None,
                                           apps[t]["start_date"], t))
                        parent_ref = (apps[targets[0]]["reference"]
                                      if targets else
                                      (unresolved_refs.get(uid) or [None])[0])
                        cp.write_row((
                            s["project_id"], uid, parent_ref,
                            uid == s["root_uid"], a["role"], bool(a["refs"]),
                            a.get("_linked_by", "reference")))
            cur.execute("TRUNCATE project_applications")
            cur.execute("INSERT INTO project_applications (project_id, uid, "
                        "parent_ref, is_root, role, has_parent_refs, "
                        "linked_by) SELECT * FROM _members")

            log.info("Inserting %s review rows", len(review_rows))
            cur.execute("CREATE TEMP TABLE _review (authority text, "
                        "project_id bigint, uid text, reason text, "
                        "candidates jsonb) ON COMMIT DROP")
            with cur.copy("COPY _review FROM STDIN") as cp:
                for auth, s, uid, reason, cands in review_rows:
                    cp.write_row((auth, s["project_id"], uid, reason,
                                  json.dumps(cands)))
            cur.execute("DELETE FROM grouping_review")
            cur.execute("INSERT INTO grouping_review (authority, project_id, "
                        "uid, reason, candidates) SELECT * FROM _review")
        maybe_commit("memberships+review")

        # Aggregates — root_uid / grouping_state deliberately untouched
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE projects p SET
                    n_applications = s.n,
                    latest_uid     = s.latest_uid,
                    name           = s.name,
                    first_seen     = s.first_seen,
                    last_updated   = s.last_updated
                FROM (
                    SELECT pa.project_id, count(*) AS n,
                        (array_agg(a.uid ORDER BY
                            coalesce(a.decided_date, a.last_updated)
                            DESC NULLS LAST, a.uid))[1] AS latest_uid,
                        (array_agg(a.address ORDER BY length(a.address))
                            FILTER (WHERE nullif(btrim(a.address), '')
                                    IS NOT NULL))[1] AS name,
                        min(a.first_seen) AS first_seen,
                        max(a.last_updated) AS last_updated
                    FROM project_applications pa
                    JOIN applications a ON a.uid = pa.uid
                    GROUP BY pa.project_id
                ) s WHERE p.id = s.project_id
            """)
            cur.execute("""
                UPDATE projects p SET root_in_db = NOT EXISTS (
                    SELECT 1 FROM project_applications pa
                    JOIN missing_parents mp ON mp.authority = p.authority
                        AND mp.reference = pa.parent_ref
                        AND mp.status <> 'found'
                    WHERE pa.project_id = p.id)
            """)
            cur.execute("SELECT count(*) FROM projects")
            stats["final_projects"] = cur.fetchone()[0]
            cur.execute("SELECT grouping_state, count(*) FROM projects "
                        "GROUP BY 1")
            stats.update({f"final_state_{k}": v for k, v in cur.fetchall()})
            cur.execute("SELECT reason, count(*) FROM grouping_review "
                        "GROUP BY 1")
            stats.update({f"queue_{k}": v for k, v in cur.fetchall()})
            stats["memberships"] = sum(len(s["members"]) for s in schemes)
    finally:
        if args.apply:
            conn.commit()
            log.info("APPLIED")
        else:
            conn.rollback()
            log.info("DRY RUN — rolled back")
        conn.close()

    print(json.dumps(dict(stats), indent=2, default=str))


if __name__ == "__main__":
    main()
