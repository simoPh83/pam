"""One-off: promote useful other_fields_json keys to real columns, drop junk keys.

Usage: python scripts/migrate_other_fields.py [--dry-run] [--vacuum]
Idempotent. Stop the worker first.
"""
import argparse
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402

DATE_RE = r"^\d{4}-\d{2}-\d{2}"
PROMOTE = {
    "application_type": "TEXT",
    "applicant_address": "TEXT",
    "n_statutory_days": "INTEGER",
    "comment_date": "DATE",
    "neighbour_consultation_start_date": "DATE",
    "neighbour_consultation_end_date": "DATE",
    "consultation_start_date": "DATE",
    "decision_published_date": "DATE",
}
DROP = ["case_officer", "easting", "northing"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vacuum", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    conn.execute("SET statement_timeout = 0")

    for col, typ in PROMOTE.items():
        conn.execute(f"ALTER TABLE applications ADD COLUMN IF NOT EXISTS {col} {typ}")
    conn.commit()

    sets, params = [], []
    for col, typ in PROMOTE.items():
        src = f"(other_fields_json->>'{col}')"
        if typ == "DATE":
            sets.append(f"{col} = CASE WHEN {src} ~ %s THEN left({src}, 10)::date END")
            params.append(DATE_RE)
        elif typ == "INTEGER":
            sets.append(f"{col} = CASE WHEN {src} ~ %s THEN {src}::numeric::integer END")
            params.append(r"^\d+(\.0+)?$")
        else:
            sets.append(f"{col} = NULLIF(btrim({src}), '')")
    keys = list(PROMOTE) + DROP
    sql = ("UPDATE applications SET " + ", ".join(sets) +
           ", other_fields_json = other_fields_json - %s::text[] "
           "WHERE authority = %s AND other_fields_json ?| %s::text[]")

    # One pass per authority, committed separately, so dead rows stay small and
    # an interruption loses nothing (rerun continues: stripped rows no longer match).
    authorities = [r[0] for r in conn.execute(
        "SELECT DISTINCT authority FROM applications ORDER BY 1")]
    for k, auth in enumerate(authorities, 1):
        rows = conn.execute(sql, (*params, keys, auth, keys)).rowcount
        bad = [c for c in PROMOTE if conn.execute(
            f"SELECT 1 FROM applications WHERE authority=%s AND {c} IS NULL "
            f"AND NULLIF(btrim(other_fields_json->>'{c}'), '') IS NOT NULL LIMIT 1",
            (auth,)).fetchone()]
        if bad:
            conn.rollback()
            sys.exit(f"{auth}: values did not parse for {bad}; rolled back this batch")
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
        print(f"[{k}/{len(authorities)}] {auth}: {rows} rows")
        if not args.dry_run and k % 4 == 0:
            conn.autocommit = True
            conn.execute("VACUUM applications")
            conn.autocommit = False
    if args.dry_run:
        print("dry run: nothing committed")
        return
    if args.vacuum:
        conn.autocommit = True
        conn.execute("VACUUM FULL applications")
        size = conn.execute(
            "SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
        print("vacuumed; db size:", size)

if __name__ == "__main__":
    main()
