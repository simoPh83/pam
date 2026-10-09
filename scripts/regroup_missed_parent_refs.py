"""One-off: regroup applications whose parent ref was missed by an older PARENT_RE.

Finds project members with no parent refs recorded whose description now
yields refs (e.g. "planning approval HGY/2022/2731") and re-runs group() on them.

Usage: python scripts/regroup_missed_parent_refs.py [--dry-run]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import group, parent_refs_of  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    conn = Ledger(os.environ["DATABASE_URL"]).conn

    rows = conn.execute(
        "SELECT pa.uid, a.description FROM project_applications pa "
        "JOIN applications a ON a.uid = pa.uid "
        "WHERE NOT pa.has_parent_refs AND a.description IS NOT NULL").fetchall()
    uids = [uid for uid, desc in rows if parent_refs_of(desc)]
    print(f"{len(uids)} of {len(rows)} ref-less members now yield parent refs")

    before = conn.execute(
        "SELECT count(*) FILTER (WHERE root_in_db), count(*) FROM projects").fetchone()
    for i in range(0, len(uids), 500):
        group(uids[i:i + 500], conn)
    after = conn.execute(
        "SELECT count(*) FILTER (WHERE root_in_db), count(*) FROM projects").fetchone()
    print(f"root_in_db before/after: {before[0]} -> {after[0]} (projects {after[1]})")

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back")
    else:
        conn.commit()
        print("committed")


if __name__ == "__main__":
    main()
