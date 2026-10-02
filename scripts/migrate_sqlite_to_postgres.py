"""One-off copy of data/ledger.sqlite into Postgres (e.g. Supabase).

Usage:
    Put DATABASE_URL=postgresql://... in .env, or set it in the shell.
    python scripts/migrate_sqlite_to_postgres.py [--sqlite data/ledger.sqlite] [--truncate]

Creates the schema via pam.ledger.Ledger, then copies applications,
state_history and fetch_progress in batches. Refuses to run if the target
already holds applications unless --truncate is given.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from pam.ledger import Ledger

BATCH = 1000


def load_dotenv(path: Path) -> None:
    """Load simple KEY=value entries without replacing existing environment variables."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def copy_table(src: sqlite3.Connection, dst, table: str, skip: set[str] = frozenset()):
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})") if r[1] not in skip]
    col_list = ", ".join(cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    cur = src.execute(f"SELECT {col_list} FROM {table}")
    total = 0
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        with dst.cursor() as c:
            c.executemany(sql, rows)
        dst.commit()
        total += len(rows)
        print(f"  {table}: {total}", end="\r")
    print(f"  {table}: {total} rows copied")
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", default="data/ledger.sqlite")
    parser.add_argument("--truncate", action="store_true",
                        help="empty the target tables first")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("Set DATABASE_URL to the Postgres connection string.", file=sys.stderr)
        return 1
    if not Path(args.sqlite).exists():
        print(f"SQLite file not found: {args.sqlite}", file=sys.stderr)
        return 1

    ledger = Ledger(url)  # creates schema
    dst = ledger.conn
    existing = dst.execute("SELECT count(*) FROM applications").fetchone()[0]
    if existing and not args.truncate:
        print(f"Target already has {existing} applications; use --truncate.",
              file=sys.stderr)
        return 1
    dst.execute("TRUNCATE applications, state_history, fetch_progress RESTART IDENTITY")
    dst.commit()

    src = sqlite3.connect(args.sqlite)
    n_apps = copy_table(src, dst, "applications")
    copy_table(src, dst, "state_history", skip={"id"})
    copy_table(src, dst, "fetch_progress")

    # Verify
    pg_apps = dst.execute("SELECT count(*) FROM applications").fetchone()[0]
    print(f"Verify: sqlite={n_apps} postgres={pg_apps}",
          "OK" if n_apps == pg_apps else "MISMATCH")
    ledger.close()
    return 0 if n_apps == pg_apps else 2


if __name__ == "__main__":
    sys.exit(main())
