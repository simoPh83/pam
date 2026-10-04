"""Apply scripts/rls.sql to Supabase and verify with the real roles.

    python scripts/apply_rls.py            # apply + verify
    python scripts/apply_rls.py --verify   # verify only
"""
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pam.config import load_dotenv  # noqa: E402

TABLES = ["applications", "state_history", "fetch_progress", "authority_urls",
          "jobs", "applications_full"]


def main() -> None:
    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    if "--verify" not in sys.argv:
        conn.execute((Path(__file__).parent / "rls.sql").read_text())
        print("applied rls.sql")

    print("worker role bypasses RLS:",
          conn.execute("select rolbypassrls or rolsuper from pg_roles "
                       "where rolname = current_user").fetchone()[0])
    for role in ("anon", "authenticated"):
        for t in TABLES:
            with conn.transaction():
                conn.execute(f"set local role {role}")
                try:
                    n = conn.execute(f"select count(*) from public.{t}").fetchone()[0]
                    res = f"select ok, {n} rows visible"
                except psycopg.errors.InsufficientPrivilege:
                    res = "select denied"
            with conn.transaction():
                conn.execute(f"set local role {role}")
                try:
                    conn.execute(f"update public.{t} set uid = uid where false"
                                 if t == "applications" else f"delete from public.{t} where false")
                    w = "write ALLOWED"
                except psycopg.errors.InsufficientPrivilege:
                    w = "write denied"
                except psycopg.errors.Error as e:
                    w = f"write n/a ({type(e).__name__})"
            print(f"{role:14} {t:18} {res:30} {w}")


if __name__ == "__main__":
    main()
