"""Read-only Supabase disk-space report: table/index sizes, index usage,
vacuum health. Re-run after big regroups/backfills to catch bloat early.

    .venv\\Scripts\\python.exe scripts\\db_size_report.py

Added 2026-10-12 after the 500 MB quota cleanup: idx_scan is cumulative
(pg_stat_database.stats_reset is NULL on this project), so 0 scans means
"never used since creation".
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pam.config import load_dotenv  # noqa: E402


def show(title, rows, cols):
    print(f"\n=== {title} ===")
    print(" | ".join(cols))
    print("-" * 100)
    for r in rows:
        print(" | ".join(str(x) for x in r))


def main() -> None:
    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    cur = conn.cursor()

    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())), "
                "pg_database_size(current_database())")
    show("DATABASE SIZE", cur.fetchall(), ["pretty", "bytes"])

    cur.execute("SELECT stats_reset FROM pg_stat_database "
                "WHERE datname = current_database()")
    show("STATS RESET (NULL = idx_scan counts are lifetime)",
         cur.fetchall(), ["stats_reset"])

    cur.execute("""
        SELECT n.nspname, c.relname,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
               pg_size_pretty(pg_relation_size(c.oid)) AS heap,
               pg_size_pretty(pg_indexes_size(c.oid)) AS indexes,
               pg_stat_get_live_tuples(c.oid) AS live_rows,
               pg_stat_get_dead_tuples(c.oid) AS dead_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r'
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY pg_total_relation_size(c.oid) DESC
        """)
    show("TABLES (by total size, incl. supabase-internal schemas)",
         cur.fetchall(),
         ["schema", "table", "total", "heap", "indexes", "live_rows", "dead_rows"])

    cur.execute("""
        SELECT s.schemaname, s.relname, s.indexrelname,
               pg_size_pretty(pg_relation_size(s.indexrelid)) AS size,
               s.idx_scan
        FROM pg_stat_user_indexes s
        WHERE s.schemaname = 'public'
        ORDER BY pg_relation_size(s.indexrelid) DESC
        """)
    show("PUBLIC INDEXES (by size; idx_scan = lifetime uses)",
         cur.fetchall(), ["schema", "table", "index", "size", "idx_scan"])

    cur.execute("""
        SELECT relname, last_autovacuum, last_autoanalyze, n_dead_tup, n_live_tup
        FROM pg_stat_user_tables
        WHERE schemaname = 'public'
        ORDER BY n_dead_tup DESC
        """)
    show("VACUUM HEALTH (high dead/live ratio = bloat building up)",
         cur.fetchall(),
         ["table", "last_autovacuum", "last_autoanalyze", "dead", "live"])

    conn.close()


if __name__ == "__main__":
    main()
