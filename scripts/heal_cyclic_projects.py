"""One-off: heal ambiguous projects that have no root candidate.

Context: sibling consents that cite each other (a Full permission and its
listed-building / advertising consent, "linked to ... under ref: X") create
reference cycles: every member's parent_ref resolves, so no member is a root
candidate. _set_root_and_state() used to fall back to "all members are
candidates", marking the project ambiguous and inserting a pending
multi_root review with an EMPTY candidates array — the UI's
"This address contains 0 candidate schemes" on a "Multiple schemes" row.

The fixed _set_root_and_state() elects the earliest member as provisional
root for this shape (and for fragments whose chain head lives in another
project) and raises no review. This script re-derives the affected projects;
refresh() resolves the stale empty review rows as 'obsolete' itself.

Idempotent; safe to re-run. Not a parser change, so no regroup is needed.

Usage: python scripts/heal_cyclic_projects.py [--dry-run]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import refresh  # noqa: E402

# Ambiguous projects where every member's stored parent_ref resolves to a
# stored application (no member is a root candidate).
NOCAND_SQL = """
SELECT p.id
FROM projects p
WHERE p.grouping_state = 'ambiguous'
  AND NOT EXISTS (
      SELECT 1
      FROM project_applications pa
      JOIN applications a ON a.uid = pa.uid
      WHERE pa.project_id = p.id
        AND (pa.parent_ref IS NULL
             OR NOT EXISTS (SELECT 1 FROM applications x
                            WHERE x.authority = a.authority
                              AND (upper(x.reference) = pa.parent_ref
                                   OR starts_with(upper(x.reference),
                                                  pa.parent_ref || '/')))))
ORDER BY p.id
"""

EMPTY_REVIEW_SQL = """
SELECT count(*) FROM grouping_review
WHERE reason = 'multi_root' AND status = 'pending'
  AND (candidates IS NULL OR jsonb_array_length(candidates) = 0)
"""

# Projects carrying a pending empty-candidates multi_root row whose stored
# refs do have a root candidate: stored parent_ref and the live parse have
# diverged, so the last refresh snapshotted zero candidates. A fresh refresh
# re-derives them (real candidates snapshot, or the cycle fallback).
EMPTY_REVIEW_PROJECTS_SQL = """
SELECT DISTINCT project_id FROM grouping_review
WHERE reason = 'multi_root' AND status = 'pending'
  AND (candidates IS NULL OR jsonb_array_length(candidates) = 0)
"""

CROSSTAB_SQL = ("SELECT grouping_state, count(*) FROM projects "
                "WHERE id = ANY(%s) GROUP BY 1 ORDER BY 1")

SPOT_SQL = ("SELECT id, root_uid, grouping_state, root_in_db, "
            "has_missing_ancestors FROM projects WHERE id = ANY(%s) "
            "ORDER BY id")

SPOT_PIDS = [101405, 652203]  # Greenwich Devonport House pair


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn
    # The NOCAND_SQL anti-join can exceed the default statement timeout.
    conn.execute("SET statement_timeout = 0")

    pids = sorted(
        {r[0] for r in conn.execute(NOCAND_SQL)}
        | {r[0] for r in conn.execute(EMPTY_REVIEW_PROJECTS_SQL)})
    print(f"ambiguous projects with no root candidate or an empty-candidates "
          f"review row: {len(pids)}")
    print("pending empty-candidates multi_root rows:",
          conn.execute(EMPTY_REVIEW_SQL).fetchone()[0])
    print("before:", conn.execute(CROSSTAB_SQL, (pids,)).fetchall())
    print("spot before:", conn.execute(SPOT_SQL, (SPOT_PIDS,)).fetchall())

    for i in range(0, len(pids), 500):
        refresh(conn, pids[i:i + 500])
        print(f"  refreshed {min(i + 500, len(pids))}/{len(pids)}",
              flush=True)

    print("after:", conn.execute(CROSSTAB_SQL, (pids,)).fetchall())
    print("spot after:", conn.execute(SPOT_SQL, (SPOT_PIDS,)).fetchall())
    print("pending empty-candidates multi_root rows after:",
          conn.execute(EMPTY_REVIEW_SQL).fetchone()[0])

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back")
    else:
        conn.commit()
        print("committed")
    ledger.close()
    print("Next: restart the worker (pam.projects changed).")


if __name__ == "__main__":
    main()
