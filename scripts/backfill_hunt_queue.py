"""One-off: close the hunt-queue gap and heal stale chain flags.

Context: docs/2026-10-19-backend-issue-stale-flags-and-hunt-queue-gap.md.

missing_parents rows are inserted by group() (pass 4b), i.e. only when an
application's membership is recomputed. Applications grouped by an older
parser (or whose parent was purged after a clean compute) carry stored
parent_refs that never reached the hunt queue, and their chain flags
(root_in_db / has_missing_ancestors) were never re-derived by the fixed
_set_root_and_state(). refresh() now enqueues unresolved refs itself, so
re-deriving every project with an unresolved stored parent_ref both
backfills missing_parents and heals the flags in one pass.

Idempotent; safe to re-run. Does not move memberships — applications whose
stored refs are missing entirely (parser now yields refs where it yielded
none) remain the job of scripts/regroup_missed_parent_refs.py.

Usage: python scripts/backfill_hunt_queue.py [--dry-run]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import refresh  # noqa: E402

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn

    print("before: (refs without queue row, projects) =",
          conn.execute(GAP_SQL).fetchone())
    print("before: projects with stale chain flags =",
          conn.execute(STALE_FLAGS_SQL).fetchone())

    pids = [r[0] for r in conn.execute(STALE_SQL)]
    print(f"refreshing {len(pids)} projects with unresolved stored refs")
    for i in range(0, len(pids), 500):
        refresh(conn, pids[i:i + 500])
        print(f"  {min(i + 500, len(pids))}/{len(pids)}", flush=True)

    print("after: (refs without queue row, projects) =",
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
    print("Next: python -m pam.worker enqueue-parent-hunt")


if __name__ == "__main__":
    main()
