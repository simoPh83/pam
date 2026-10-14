"""One-off: heal stale missing_parents rows and re-derive project chain flags.

Context: docs/2026-10-14-backend-issue-root-in-db-vs-grouping-state.md.

root_in_db used to be computed from the missing_parents table, which is only
reconciled by the parent hunt — rows went stale when a cited parent arrived
via ordinary sync/backfill, leaving root_in_db = false on otherwise-clean
projects. Both flags are now derived live in projects._set_root_and_state()
(has_missing_ancestors = any member cites an unresolvable ref; root_in_db is
its negation). This script:

1. deletes missing_parents rows whose ref now resolves in applications
   (the same rule the parent hunt's local-first lookup uses),
2. recomputes flags for every project that could carry stale values
   (root_in_db not true, or grouping_state not 'clean'),
3. backfills has_missing_ancestors = false for the untouched clean remainder.

Idempotent; safe to re-run.

Usage: python scripts/refresh_project_flags.py [--dry-run]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import refresh  # noqa: E402

HEAL_SQL = """
DELETE FROM missing_parents mp
WHERE EXISTS (SELECT 1 FROM applications a
              WHERE a.authority = mp.authority
                AND (upper(a.reference) = mp.reference
                     OR starts_with(upper(a.reference), mp.reference || '/')))
"""

CROSSTAB_SQL = ("SELECT grouping_state, root_in_db, has_missing_ancestors, "
                "count(*) FROM projects GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn

    print("before (grouping_state, root_in_db, has_missing_ancestors, n):")
    for row in conn.execute(CROSSTAB_SQL):
        print("  ", row)

    healed = conn.execute(HEAL_SQL).rowcount
    print(f"healed {healed} stale missing_parents row(s) "
          "(ref now resolves in applications)")

    pids = [r[0] for r in conn.execute(
        "SELECT id FROM projects WHERE root_in_db IS DISTINCT FROM true "
        "OR grouping_state <> 'clean' ORDER BY id")]
    print(f"re-deriving flags for {len(pids)} projects")
    for i in range(0, len(pids), 500):
        refresh(conn, pids[i:i + 500])
        print(f"  {min(i + 500, len(pids))}/{len(pids)}")

    # Remainder: clean projects not re-derived since the column was added —
    # their last compute had no unresolved refs, so false is correct.
    conn.execute("UPDATE projects SET has_missing_ancestors = false "
                 "WHERE has_missing_ancestors IS NULL")

    print("after (grouping_state, root_in_db, has_missing_ancestors, n):")
    for row in conn.execute(CROSSTAB_SQL):
        print("  ", row)

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back")
    else:
        conn.commit()
        print("committed")
    ledger.close()


if __name__ == "__main__":
    main()
