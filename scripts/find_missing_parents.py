"""One-off: catalogue unresolved parent refs already in project_applications.

Backfills missing_parents from existing data (ongoing detection happens in
projects.group() from now on), then prints how to enqueue the hunt jobs.

Usage: python scripts/find_missing_parents.py [--dry-run]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402
from pam.projects import ref_year_of  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    load_dotenv(Path(".env"))
    ledger = Ledger(os.environ["DATABASE_URL"])
    conn = ledger.conn

    rows = conn.execute(
        """
        SELECT DISTINCT a.authority, pa.parent_ref, pa.uid, pa.project_id
        FROM project_applications pa
        JOIN applications a ON a.uid = pa.uid
        WHERE pa.parent_ref IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM applications p
                          WHERE p.authority = a.authority
                            AND upper(p.reference) = pa.parent_ref)
        """).fetchall()

    params = [(auth, ref, ref_year_of(ref), uid, pid)
              for auth, ref, uid, pid in rows]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO missing_parents "
            "(authority, reference, ref_year, requested_by, project_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (authority, reference) DO NOTHING", params)

    stats = conn.execute(
        "SELECT status, count(*), count(ref_year) FROM missing_parents "
        "GROUP BY 1 ORDER BY 1").fetchall()
    # refresh root_in_db now that missing_parents is populated
    conn.execute(
        "UPDATE projects p SET root_in_db = NOT EXISTS ("
        "  SELECT 1 FROM project_applications pa "
        "  JOIN missing_parents mp ON mp.authority = p.authority "
        "    AND mp.reference = pa.parent_ref AND mp.status <> 'found'"
        "  WHERE pa.project_id = p.id)")
    roots = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE root_in_db) FROM projects"
    ).fetchone()

    if args.dry_run:
        conn.rollback()
        print("dry run — rolled back")
    else:
        conn.commit()
    print("missing_parents:", stats)
    print(f"projects: {roots[0]}, root_in_db: {roots[1]}")
    print("Next: python -m pam.worker enqueue-parent-hunt --months 9")


if __name__ == "__main__":
    main()
