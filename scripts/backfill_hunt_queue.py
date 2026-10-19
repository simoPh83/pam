"""One-off: close the hunt-queue gap and heal stale chain flags.

Context: docs/2026-10-19-backend-issue-stale-flags-and-hunt-queue-gap.md.

missing_parents rows are inserted by group() (pass 4b), i.e. only when an
application's membership is recomputed. Applications grouped by an older
parser (or whose parent was purged after a clean compute) carry stored
parent_refs that never reached the hunt queue. Projects with a stale
has_missing_ancestors flag but no unresolved stored parent_ref also need a
refresh: the current parser may find a live ref in the description, or the
flag may clear. refresh() both re-derives the flags and enqueues unresolved
refs in one pass.

Idempotent; safe to re-run. Does not move memberships — applications whose
stored refs are missing entirely (parser now yields refs where it yielded
none) remain the job of scripts/regroup_missed_parent_refs.py.

Usage: python scripts/backfill_hunt_queue.py [--dry-run] [--target-brief]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import parent_refs_of, ref_year_of, refresh  # noqa: E402

# Projects with at least one member whose stored parent_ref resolves to no
# stored application (the doc's scale query, without the queue-row join).
STALE_SQL = """
SELECT DISTINCT pa.project_id
FROM project_applications pa
JOIN applications a ON a.uid = pa.uid
WHERE pa.parent_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM applications p
                  WHERE p.authority = a.authority
                    AND (upper(p.reference) = pa.parent_ref
                         OR starts_with(upper(p.reference), pa.parent_ref || '/')))
ORDER BY 1
"""

FLAGGED_WITHOUT_STORED_REF_SQL = """
SELECT p.id
FROM projects p
WHERE p.grouping_state = 'provisional'
  AND p.has_missing_ancestors IS TRUE
  AND NOT EXISTS (SELECT 1 FROM missing_parents m WHERE m.project_id = p.id)
  AND NOT EXISTS (
      SELECT 1
      FROM project_applications pa
      JOIN applications a ON a.uid = pa.uid
      WHERE pa.project_id = p.id
        AND pa.parent_ref IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM applications p2
                        WHERE p2.authority = a.authority
                          AND (upper(p2.reference) = pa.parent_ref
                               OR starts_with(upper(p2.reference),
                                              pa.parent_ref || '/'))))
ORDER BY p.id
"""

FLAGGED_TARGETS_SQL = """
SELECT p.id
FROM projects p
WHERE p.grouping_state = 'provisional'
  AND p.has_missing_ancestors IS TRUE
  AND NOT EXISTS (SELECT 1 FROM missing_parents m WHERE m.project_id = p.id)
ORDER BY p.id
"""

TARGET_STORED_REFS_SQL = """
SELECT DISTINCT ON (a.authority, pa.parent_ref)
       a.authority, pa.parent_ref, pa.uid, pa.project_id
FROM project_applications pa
JOIN applications a ON a.uid = pa.uid
WHERE pa.project_id = ANY(%s)
  AND pa.parent_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM applications p
                  WHERE p.authority = a.authority
                    AND (upper(p.reference) = pa.parent_ref
                         OR starts_with(upper(p.reference),
                                        pa.parent_ref || '/')))
ORDER BY a.authority, pa.parent_ref, pa.project_id, pa.uid
"""

TARGET_GAP_SQL = """
SELECT count(DISTINCT (a.authority, pa.parent_ref)),
       count(DISTINCT pa.project_id)
FROM project_applications pa
JOIN applications a ON a.uid = pa.uid
WHERE pa.project_id = ANY(%s)
  AND pa.parent_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM applications p
                  WHERE p.authority = a.authority
                    AND (upper(p.reference) = pa.parent_ref
                         OR starts_with(upper(p.reference),
                                        pa.parent_ref || '/')))
  AND NOT EXISTS (SELECT 1 FROM missing_parents m
                  WHERE m.authority = a.authority
                    AND m.reference = pa.parent_ref)
"""

UNQUEUED_STORED_REF_PROJECTS_SQL = """
SELECT DISTINCT pa.project_id
FROM project_applications pa
JOIN applications a ON a.uid = pa.uid
WHERE pa.parent_ref IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM applications p
                  WHERE p.authority = a.authority
                    AND (upper(p.reference) = pa.parent_ref
                         OR starts_with(upper(p.reference),
                                        pa.parent_ref || '/')))
  AND NOT EXISTS (SELECT 1 FROM missing_parents m
                  WHERE m.authority = a.authority
                    AND m.reference = pa.parent_ref)
ORDER BY pa.project_id
"""

GROUP_COUNTS_SQL = """
WITH target AS (
  SELECT p.id,
         EXISTS (
           SELECT 1
           FROM project_applications pa
           JOIN applications a ON a.uid = pa.uid
           WHERE pa.project_id = p.id
             AND pa.parent_ref IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM applications p2
                             WHERE p2.authority = a.authority
                               AND (upper(p2.reference) = pa.parent_ref
                                    OR starts_with(upper(p2.reference),
                                                   pa.parent_ref || '/')))
         ) AS has_unresolved_stored_ref
  FROM projects p
  WHERE p.grouping_state = 'provisional'
    AND p.has_missing_ancestors IS TRUE
    AND NOT EXISTS (SELECT 1 FROM missing_parents m WHERE m.project_id = p.id)
)
SELECT has_unresolved_stored_ref, count(*)
FROM target
GROUP BY has_unresolved_stored_ref
ORDER BY has_unresolved_stored_ref DESC
"""

GAP_SQL = """
WITH unresolved AS (
  SELECT DISTINCT pa.project_id, a.authority, pa.parent_ref
  FROM project_applications pa
  JOIN applications a ON a.uid = pa.uid
  WHERE pa.parent_ref IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM applications p
                    WHERE p.authority = a.authority
                      AND (upper(p.reference) = pa.parent_ref
                           OR starts_with(upper(p.reference), pa.parent_ref || '/')))
)
SELECT count(*) FILTER (WHERE mp.reference IS NULL),
       count(DISTINCT u.project_id) FILTER (WHERE mp.reference IS NULL)
FROM unresolved u
LEFT JOIN missing_parents mp
  ON mp.authority = u.authority AND mp.reference = u.parent_ref
"""

STALE_FLAGS_SQL = """
SELECT count(DISTINCT pa.project_id)
FROM project_applications pa
JOIN applications a ON a.uid = pa.uid
JOIN projects p ON p.id = pa.project_id
WHERE pa.parent_ref IS NOT NULL
  AND p.has_missing_ancestors = false
  AND NOT EXISTS (SELECT 1 FROM applications p2
                  WHERE p2.authority = a.authority
                    AND (upper(p2.reference) = pa.parent_ref
                         OR starts_with(upper(p2.reference), pa.parent_ref || '/')))
"""

GROUP_B_FLAGS_SQL = """
SELECT id, grouping_state, root_in_db, has_missing_ancestors
FROM projects
WHERE id = ANY(%s)
ORDER BY id
"""


def live_unresolved_refs(conn, pids):
    """Return unresolved refs parsed from descriptions or recorded manually."""
    if not pids:
        return []

    members = conn.execute(
        "SELECT pa.project_id, pa.uid, a.authority, a.description "
        "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
        "WHERE pa.project_id = ANY(%s)", (pids,)).fetchall()
    manual_refs = {}
    uids = [row[1] for row in members]
    if uids:
        for uid, ref in conn.execute(
                "SELECT uid, reference FROM manual_parent_refs "
                "WHERE uid = ANY(%s)", (uids,)):
            manual_refs.setdefault(uid, set()).add(ref)

    sources = {}
    refs_by_authority = {}
    for pid, uid, authority, description in members:
        for ref in parent_refs_of(description, authority):
            sources[(pid, uid, authority, ref)] = "description"
            refs_by_authority.setdefault(authority, set()).add(ref)
        for ref in manual_refs.get(uid, ()):
            key = (pid, uid, authority, ref)
            sources[key] = ("description + manual"
                            if key in sources else "manual")
            refs_by_authority.setdefault(authority, set()).add(ref)

    unresolved_by_authority = {}
    for authority, refs in refs_by_authority.items():
        if refs:
            unresolved_by_authority[authority] = {
                row[0] for row in conn.execute(
                    "SELECT cand.ref FROM unnest(%s::text[]) AS cand(ref) "
                    "WHERE NOT EXISTS (SELECT 1 FROM applications a "
                    "                  WHERE a.authority = %s "
                    "                    AND (upper(a.reference) = cand.ref "
                    "                         OR starts_with(upper(a.reference), "
                    "                                        cand.ref || '/')))",
                    (list(refs), authority))}

    unresolved_pairs = {
        (authority, ref)
        for authority, refs in unresolved_by_authority.items()
        for ref in refs
    }
    status_by_pair = {}
    if unresolved_pairs:
        pairs = sorted(unresolved_pairs)
        for authority, ref, status in conn.execute(
                "SELECT candidate.authority, candidate.reference, mp.status "
                "FROM unnest(%s::text[], %s::text[]) "
                "AS candidate(authority, reference) "
                "LEFT JOIN missing_parents mp "
                "ON mp.authority = candidate.authority "
                "AND mp.reference = candidate.reference",
                ([authority for authority, _ in pairs],
                 [ref for _, ref in pairs])):
            status_by_pair[(authority, ref)] = status or "missing"

    found = [
        (pid, uid, ref, source, status_by_pair[(authority, ref)])
        for (pid, uid, authority, ref), source in sources.items()
        if (authority, ref) in unresolved_pairs
    ]
    return sorted(found)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--target-brief", action="store_true",
        help="refresh only provisional flagged projects without a project-linked hunt row")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn

    print("flagged provisional projects without a project-linked hunt row "
          "(unresolved stored ref, count):",
          conn.execute(GROUP_COUNTS_SQL).fetchall())
    print("before: (unresolved refs without queue row, projects) =",
          conn.execute(GAP_SQL).fetchone())
    print("before: projects with stale chain flags =",
          conn.execute(STALE_FLAGS_SQL).fetchone())

    group_b = [r[0] for r in conn.execute(FLAGGED_WITHOUT_STORED_REF_SQL)]
    before_group_b = {
        row[0]: row[1:]
        for row in conn.execute(GROUP_B_FLAGS_SQL, (group_b,)).fetchall()
    } if group_b else {}
    if args.target_brief:
        pids = [r[0] for r in conn.execute(FLAGGED_TARGETS_SQL)]
        group_b_set = set(group_b)
        group_a = [pid for pid in pids if pid not in group_b_set]
        residual_pids = [r[0] for r in
                         conn.execute(UNQUEUED_STORED_REF_PROJECTS_SQL)]
        ref_project_ids = sorted(set(group_a) | set(residual_pids))
        refs = conn.execute(TARGET_STORED_REFS_SQL,
                            (ref_project_ids,)).fetchall()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO missing_parents "
                "(authority, reference, ref_year, requested_by, project_id) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (authority, reference) DO NOTHING",
                [(auth, ref, ref_year_of(ref), uid, pid)
                 for auth, ref, uid, pid in refs])
            inserted_refs = cur.rowcount
        print(f"catalogued {inserted_refs} previously unqueued stored ref(s) "
              f"across {len(group_a)} Group A and {len(residual_pids)} "
              "related residual project(s)")
        pids = sorted(set(group_b) | set(residual_pids))
        print(f"refreshing {len(group_b)} Group B and "
              f"{len(residual_pids)} residual project(s)")
    else:
        pids = sorted({r[0] for r in conn.execute(STALE_SQL)} | set(group_b))
        print(f"refreshing {len(pids)} projects with unresolved stored refs "
              "or a stale provisional chain flag")
    for i in range(0, len(pids), 500):
        refresh(conn, pids[i:i + 500])
        print(f"  {min(i + 500, len(pids))}/{len(pids)}", flush=True)

    if args.target_brief:
        print("Group A unresolved stored refs without any queue row (refs, projects):",
              conn.execute(TARGET_GAP_SQL, (group_a,)).fetchone())

    after_group_b = {
        row[0]: row[1:]
        for row in conn.execute(GROUP_B_FLAGS_SQL, (group_b,)).fetchall()
    } if group_b else {}
    changed_group_b = [
        (pid, before_group_b[pid], after_group_b[pid])
        for pid in group_b if before_group_b[pid] != after_group_b[pid]
    ]
    print(f"Group B flags changed for {len(changed_group_b)}/{len(group_b)} "
          "projects (state, root_in_db, has_missing_ancestors):")
    for pid, before, after in changed_group_b:
        print(f"  {pid}: {before} -> {after}")

    remaining_group_b = [
        pid for pid in group_b if after_group_b[pid][2] is True
    ]
    remaining_refs = live_unresolved_refs(conn, remaining_group_b)
    print("Group B still flagged after refresh "
          "(project, citing uid, live ref, source, hunt status):")
    for row in remaining_refs:
        print("  ", row)
    if remaining_group_b and not remaining_refs:
        print("  none found by the current description/manual-ref parser")

    print("after flagged provisional projects without a project-linked hunt row "
          "(unresolved stored ref, count):",
          conn.execute(GROUP_COUNTS_SQL).fetchall())
    print("after: (unresolved refs without queue row, projects) =",
          conn.execute(GAP_SQL).fetchone())
    print("after: projects with stale chain flags =",
          conn.execute(STALE_FLAGS_SQL).fetchone())

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back")
    else:
        conn.commit()
        print("committed")
    ledger.close()
    print("Next: restart the worker — with AUTO_PARENT_HUNT=1 the nightly "
          "cycle enqueues the hunts automatically. To start them now instead: "
          "python -m pam.worker enqueue-parent-hunt")


if __name__ == "__main__":
    main()
