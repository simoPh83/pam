"""One-off: group all existing applications into projects, per authority.

Usage: python scripts/backfill_projects.py [--authority NAME] [--dry-run]
Idempotent; safe to rerun. Stop the worker first (or run when idle).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam import projects  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--authority")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))

    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn
    conn.execute("SET statement_timeout = 0")

    where = "WHERE authority = %s" if args.authority else ""
    params = (args.authority,) if args.authority else ()
    authorities = [r[0] for r in conn.execute(
        f"SELECT DISTINCT authority FROM applications {where} ORDER BY 1", params)]

    for i, auth in enumerate(authorities, 1):
        uids = [r[0] for r in conn.execute(
            "SELECT uid FROM applications WHERE authority = %s", (auth,))]
        n = projects.group(uids, conn)
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
        print(f"[{i}/{len(authorities)}] {auth}: {n} grouped")

    done = "rolled back (dry run)" if args.dry_run else "committed"
    print(conn.execute(
        "SELECT count(*) FROM projects").fetchone()[0], "projects;", done)


if __name__ == "__main__":
    main()
