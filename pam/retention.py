"""Rolling retention window: keep the DB slim by dropping stale projects.

A project is dropped when its last_updated (latest real PlanIt activity in the
chain) is older than RETENTION_MONTHS. Its applications go with it, as do
dangling missing_parents / state_history rows. Starred or annotated projects
are always kept.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("pam.retention")

RETENTION_MONTHS = int(os.environ.get("RETENTION_MONTHS", "16"))
BATCH = 5000
# Ungrouped applications are brand new (grouping runs after a fetch); give the
# grouping pass time to claim them before treating them as orphans.
UNGROUPED_GRACE_DAYS = 7


def _protected_sql(conn) -> str:
    parts = [
        f"SELECT project_id FROM {t}"
        for t in ("ui_project_stars", "ui_project_meta")
        if conn.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0]
    ]
    return " UNION ".join(parts) or "SELECT NULL::bigint WHERE false"


def prune(conn, months: int = RETENTION_MONTHS) -> dict:
    cutoff_sql = "(current_date - make_interval(months => %s))::date"
    stale = [r[0] for r in conn.execute(
        f"SELECT id FROM projects WHERE last_updated < {cutoff_sql} "
        f"AND id NOT IN ({_protected_sql(conn)})", (months,))]
    for i in range(0, len(stale), BATCH):
        conn.execute("DELETE FROM projects WHERE id = ANY(%s)",
                     (stale[i:i + BATCH],))
    conn.execute("DELETE FROM missing_parents WHERE project_id IS NULL")
    apps = conn.execute(
        f"DELETE FROM applications a WHERE a.last_updated < {cutoff_sql} "
        f"AND a.first_seen < current_date - %s "
        "AND NOT EXISTS (SELECT 1 FROM project_applications pa WHERE pa.uid = a.uid)",
        (months, UNGROUPED_GRACE_DAYS)).rowcount
    conn.execute("DELETE FROM state_history sh WHERE NOT EXISTS "
                 "(SELECT 1 FROM applications a WHERE a.uid = sh.uid)")
    stats = {"pruned_projects": len(stale), "pruned_applications": apps}
    if stale or apps:
        log.info("Retention (%d months): %s", months, stats)
    return stats
