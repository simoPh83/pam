"""Rolling retention window: keep the DB slim by dropping stale projects.

A project is dropped when its last_updated (latest real PlanIt activity in the
chain) is older than RETENTION_MONTHS, and its member applications go with it
in the same pass (project.last_updated is the max of theirs, so they are past
the window too). Starred or annotated projects are always kept, as is any
application also claimed by a surviving project. Applications that never
grouped are pruned once stale and past a short first-seen grace; dangling
missing_parents / state_history rows go with their owners.
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
    apps = 0
    for i in range(0, len(stale), BATCH):
        batch = stale[i:i + BATCH]
        # Member applications go with the project, same pass:
        # project.last_updated is the max of theirs, so they are all past
        # the window too. Keep any app a surviving project also claims.
        apps += conn.execute(
            "DELETE FROM applications a WHERE a.uid IN ("
            "  SELECT uid FROM project_applications WHERE project_id = ANY(%s)) "
            "AND NOT EXISTS (SELECT 1 FROM project_applications pa "
            "                WHERE pa.uid = a.uid AND pa.project_id <> ALL(%s))",
            (batch, batch)).rowcount
        conn.execute("DELETE FROM projects WHERE id = ANY(%s)", (batch,))
    conn.execute("DELETE FROM missing_parents WHERE project_id IS NULL")
    # Legacy 'found' rows: hunts now delete resolved refs outright (worker.py)
    conn.execute("DELETE FROM missing_parents WHERE status = 'found'")
    # Orphans: apps that never grouped (no usable address) or lost their
    # project in an earlier pass. The 7-day first-seen grace gives freshly
    # fetched apps time to be claimed by the grouping pass.
    apps += conn.execute(
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
