"""Convert text-typed columns to proper Postgres types (idempotent).

    python scripts/migrate_types.py --dry-run   # runs everything, then rolls back
    python scripts/migrate_types.py             # apply
    python scripts/migrate_types.py --vacuum    # also VACUUM FULL afterwards

STOP THE WORKER FIRST and deploy the matching code before restarting it.
All public views are saved, dropped, and recreated afterwards (options and
grants preserved), because Postgres cannot alter a column a view depends on.
"""
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pam.config import load_dotenv  # noqa: E402

DATES = ("start_date decided_date first_seen last_updated permission_expires "
         "date_received date_validated target_decision_date consultation_end_date "
         "application_expires_date decision_issued_date appeal_date "
         "appeal_decision_date").split()
TIMESTAMPS = "last_changed last_different last_scraped".split()
FLOATS = "lat lng distance_km".split()

# table -> {column: (target data_type, USING expression)}
PLAN = {
    "applications": {
        **{c: ("date", f"NULLIF({c}, '')::date") for c in DATES},
        **{c: ("timestamp with time zone",
               f"NULLIF({c}, '')::timestamp AT TIME ZONE 'UTC'") for c in TIMESTAMPS},
        **{c: ("double precision", f"{c}::text::double precision") for c in FLOATS},
        "in_leads_sheet": ("boolean", "in_leads_sheet <> 0"),
        "other_fields_json": ("jsonb", "NULLIF(other_fields_json, '')::jsonb"),
    },
    "state_history": {
        "observed_at": ("timestamp with time zone",
                        "observed_at::timestamp AT TIME ZONE 'UTC'"),
        "decided_date": ("date", "NULLIF(decided_date, '')::date"),
        "last_different": ("timestamp with time zone",
                           "NULLIF(last_different, '')::timestamp AT TIME ZONE 'UTC'"),
    },
    "fetch_progress": {"done": ("boolean", "done <> 0")},
}


def save_views(conn):
    views = conn.execute(
        "SELECT c.oid, c.relname, pg_get_viewdef(c.oid, true), c.reloptions "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'v'").fetchall()
    out = []
    for oid, name, definition, opts in views:
        acl = conn.execute(
            "SELECT CASE WHEN grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(grantee) END, "
            "privilege_type FROM aclexplode((SELECT relacl FROM pg_class WHERE oid = %s))",
            (oid,)).fetchall()
        out.append((name, definition, opts or [], acl))
    return out


def restore_views(conn, views):
    pending = list(views)
    while pending:
        failed = []
        for name, definition, opts, acl in pending:
            conn.execute("SAVEPOINT v")
            try:
                conn.execute(f'CREATE VIEW public."{name}" AS {definition}')
            except psycopg.errors.UndefinedTable:
                conn.execute("ROLLBACK TO SAVEPOINT v")
                failed.append((name, definition, opts, acl))
                continue
            conn.execute("RELEASE SAVEPOINT v")
            if opts:
                conn.execute(f'ALTER VIEW public."{name}" SET ({", ".join(opts)})')
            conn.execute(f'REVOKE ALL ON public."{name}" FROM PUBLIC')
            for grantee, priv in acl:
                who = "PUBLIC" if grantee == "PUBLIC" else f'"{grantee}"'
                conn.execute(f'GRANT {priv} ON public."{name}" TO {who}')
            print(f"  recreated view {name}")
        if len(failed) == len(pending):
            raise RuntimeError(f"cannot recreate views: {[f[0] for f in failed]}")
        pending = failed


def main() -> None:
    dry = "--dry-run" in sys.argv
    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"])
    conn.execute("SET statement_timeout = 0")

    views = save_views(conn)
    print("views:", [v[0] for v in views])
    if views:
        conn.execute("DROP VIEW " + ", ".join(f'public."{v[0]}"' for v in views))

    for table, cols in PLAN.items():
        current = dict(conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=%s", (table,)).fetchall())
        todo = {c: spec for c, spec in cols.items() if current.get(c) not in (spec[0], None)}
        if not todo:
            print(f"{table}: already converted")
            continue
        clauses = []
        for c, (typ, using) in todo.items():
            if c in ("in_leads_sheet", "done"):
                clauses.append(f"ALTER COLUMN {c} DROP DEFAULT")
            clauses.append(f"ALTER COLUMN {c} TYPE {typ} USING {using}")
        for c in ("in_leads_sheet", "done"):
            if c in todo:
                clauses.append(f"ALTER COLUMN {c} SET DEFAULT false")
        before = conn.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
        conn.execute(f"ALTER TABLE public.{table} " + ", ".join(clauses))
        after = conn.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
        print(f"{table}: converted {len(todo)} columns "
              f"({before / 2**20:.0f} MB -> {after / 2**20:.0f} MB, before VACUUM)")

    restore_views(conn, views)
    if dry:
        conn.rollback()
        print("dry run: rolled back")
    else:
        conn.commit()
        print("committed")
    if "--vacuum" in sys.argv and not dry:
        conn.autocommit = True
        for t in PLAN:
            conn.execute(f"VACUUM FULL public.{t}")
        print("vacuumed; db size:",
              conn.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0])


if __name__ == "__main__":
    main()
