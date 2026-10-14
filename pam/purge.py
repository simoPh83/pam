"""Purge minor-work projects, classified by the MAIN application's description.

Shared by scripts/purge_minor_projects.py (retroactive sweep) and the
grouping-time verdict (pam.main.execute / pam.worker.run_parent_hunt): when
a project's main application turns out to be minor, the whole project goes.

Rules (docs/2026-10-14-minor-works-filter-design.md):
- main = earliest member with role='original'. No original member => the
  main was never fetched => POSTPONE (never auto-purged here).
- protected: projects with ui_project_stars / ui_project_meta /
  ui_project_practices rows are never purged (user-curated; superset of the
  retention rule).
- every purge writes tombstones (pam.dismissals) for ALL member references
  + the address key, so the scheme is never re-ingested.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from .classify import is_minor, minor_hits
from .dismissals import Dismissals

log = logging.getLogger(__name__)

BATCH = 1000
# UI tables with per-project rows; deleted explicitly (no FK cascade).
_UI_FOLLOWERS = ("ui_project_root_tsv", "ui_project_agent_presence",
                 "ui_project_practices")
# UI tables whose presence protects a project from purging (user curation).
_UI_PROTECTORS = ("ui_project_stars", "ui_project_meta", "ui_project_practices")


def _existing_tables(conn, names) -> list[str]:
    return [t for t in names
            if conn.execute("SELECT to_regclass(%s)", (f"public.{t}",))
            .fetchone()[0]]


def _protected_sql(conn) -> str:
    parts = [f"SELECT project_id FROM {t}"
             for t in _existing_tables(conn, _UI_PROTECTORS)]
    return " UNION ".join(parts) or "SELECT NULL::bigint WHERE false"


def iter_verdicts(conn, project_ids=None):
    """Yield (project_row, verdict, reason) per project.

    project_row: (id, authority, address_key, n_applications, main_uid,
                  main_reference, main_description, main_app_type,
                  main_app_size, main_n_dwellings, has_original)
    verdict: 'purge' | 'keep' | 'postpone' | 'protected'
    """
    where = ""
    params: tuple = ()
    if project_ids is not None:
        if not project_ids:
            return
        where = "WHERE p.id = ANY(%s)"
        params = (sorted(project_ids),)
    protected = _protected_sql(conn)
    rows = conn.execute(
        f"""
        SELECT DISTINCT ON (p.id)
               p.id, p.authority, p.address_key, p.n_applications,
               a.uid, a.reference, a.description, a.app_type, a.app_size,
               a.n_dwellings, (pa.role = 'original') AS has_original,
               (p.id IN ({protected})) AS is_protected
        FROM projects p
        JOIN project_applications pa ON pa.project_id = p.id
        JOIN applications a ON a.uid = pa.uid
        {where}
        ORDER BY p.id, (pa.role = 'original') DESC,
                 a.start_date ASC NULLS LAST, a.uid
        """, params)
    for r in rows:
        (pid, authority, address_key, n_apps, main_uid, main_ref, desc,
         app_type, app_size, n_dwellings, has_original, is_protected) = r
        row = {"id": pid, "authority": authority, "address_key": address_key,
               "n_applications": n_apps, "main_uid": main_uid,
               "main_reference": main_ref, "main_desc": desc}
        if is_protected:
            yield row, "protected", None
        elif not has_original:
            yield row, "postpone", None   # main never fetched: no verdict
        elif is_minor(desc, app_type, app_size, n_dwellings):
            yield row, "purge", ",".join(minor_hits(desc, app_type))
        else:
            yield row, "keep", None


def purge_projects(conn, dismissed: Dismissals | None, project_ids,
                   reason_by_pid: dict[int, str] | None = None,
                   dry_run: bool = False) -> dict:
    """Purge the given projects with all members, writing tombstones first.
    Mirrors retention's dual-membership guard: an application also claimed
    by a surviving project is kept."""
    stats = defaultdict(int)
    pids = sorted(set(int(p) for p in project_ids))
    if not pids:
        return stats
    ui_tables = _existing_tables(conn, _UI_FOLLOWERS)
    for i in range(0, len(pids), BATCH):
        batch = pids[i:i + BATCH]
        members = conn.execute(
            "SELECT pa.project_id, pa.uid, upper(a.reference) "
            "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
            "WHERE pa.project_id = ANY(%s)", (batch,)).fetchall()
        by_pid: dict[int, list[tuple[str, str | None]]] = defaultdict(list)
        for pid, uid, ref in members:
            by_pid[pid].append((uid, ref))
        if dry_run:
            stats["projects"] += len(batch)
            stats["applications"] += len(members)
            continue
        for pid in batch:
            prow = conn.execute(
                "SELECT authority, address_key, n_applications FROM projects "
                "WHERE id = %s", (pid,)).fetchone()
            if not prow:
                continue
            refs = [r for _u, r in by_pid.get(pid, []) if r]
            (dismissed or Dismissals(set(), set())).dismiss(
                conn, authority=prow[0], address_key=prow[1], references=refs,
                reason=(reason_by_pid or {}).get(pid, "minor"),
                n_members=prow[2])
        apps = conn.execute(
            "DELETE FROM applications a WHERE a.uid IN ("
            "  SELECT uid FROM project_applications WHERE project_id = ANY(%s)) "
            "AND NOT EXISTS (SELECT 1 FROM project_applications pa "
            "                WHERE pa.uid = a.uid AND pa.project_id <> ALL(%s))",
            (batch, batch)).rowcount
        conn.execute("DELETE FROM missing_parents WHERE project_id = ANY(%s)",
                     (batch,))
        for t in ui_tables:
            conn.execute(f"DELETE FROM {t} WHERE project_id = ANY(%s)", (batch,))
        # projects cascade to project_applications / project_aliases /
        # grouping_review (FK ON DELETE CASCADE)
        conn.execute("DELETE FROM projects WHERE id = ANY(%s)", (batch,))
        stats["projects"] += len(batch)
        stats["applications"] += apps
        conn.commit()
        log.info("purge batch: %d projects, %d applications (running: %s)",
                 len(batch), apps, dict(stats))
    if not dry_run:
        stats["state_history_orphans"] = conn.execute(
            "DELETE FROM state_history sh WHERE NOT EXISTS "
            "(SELECT 1 FROM applications a WHERE a.uid = sh.uid)").rowcount
        conn.commit()
    return stats


def purge_touched(conn, touched_uids) -> dict:
    """Grouping-time verdict: classify the mains of projects touched by this
    run and purge the minors (covers mains arriving late via parent hunts /
    backfills, which bypass the ingest filter). Returns purge stats incl.
    the purged uids so callers can drop them from alert sets."""
    pids = [r[0] for r in conn.execute(
        "SELECT DISTINCT project_id FROM project_applications "
        "WHERE uid = ANY(%s)", (list(touched_uids),))]
    if not pids:
        return {}
    purge_pids, reasons = [], {}
    for row, verdict, reason in iter_verdicts(conn, pids):
        if verdict == "purge":
            purge_pids.append(row["id"])
            reasons[row["id"]] = reason or "minor"
    if not purge_pids:
        return {}
    purged_uids = {r[0] for r in conn.execute(
        "SELECT uid FROM project_applications WHERE project_id = ANY(%s)",
        (purge_pids,))}
    dismissed = Dismissals.load(conn)
    stats = purge_projects(conn, dismissed, purge_pids, reasons)
    stats["purged_uids"] = purged_uids
    log.info("Grouping-time verdict: purged %d minor projects of %d touched",
             len(purge_pids), len(pids))
    return stats
