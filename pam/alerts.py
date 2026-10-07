"""Stars + alerts — worker side (spec §4, docs/2026.10.05-backend-spec-new-features.md).

Stars are project-level (`ui_project_stars`); alerts are generated into
`ui_alerts` from each user's `ui_alert_rules`. The ui_* tables are created
and RLS'd by the client repo's migrations — this module ships dark: if they
don't exist yet it logs once and does nothing, so the worker never blocks
the client rollout.

generate() runs at the end of each fetch run (after projects.group): for
every starred project touched by the run, evaluate the project's members
against each starring user's enabled rules and insert due alerts. Inserts
are idempotent via ui_alerts' UNIQUE (user_id, rule_id, uid).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger(__name__)

UI_TABLES = ("ui_project_stars", "ui_alert_rules", "ui_alerts")

# Trigger on events from the last few days, not only exactly today: PlanIt
# publishes filings/decisions some days late, so "== today" would miss them.
# Idempotency comes from the unique constraint, so a wider net is safe.
RECENT_DAYS = 3

_LABELS = {
    "nma_filed": "Non-material amendment filed",
    "nma_approved": "Non-material amendment approved",
    "large_approved": "Large application approved",
    "discharge_decided": "Discharge of conditions decided",
}

_missing_logged = False


def _tables_exist(conn) -> bool:
    global _missing_logged
    missing = [t for t in UI_TABLES
               if conn.execute("SELECT to_regclass(%s)",
                               (f"public.{t}",)).fetchone()[0] is None]
    if missing:
        if not _missing_logged:
            log.info("Alert tables absent (%s) — skipping alert generation",
                     ", ".join(missing))
            _missing_logged = True
        return False
    return True


def _trigger_date(kind: str, role: str | None, app_state: str | None,
                  app_size: str | None, start_date: date | None,
                  decided_date: date | None, rule_size: str | None,
                  lead_states: list[str]) -> date | None:
    """The event date if this project member triggers the rule, else None."""
    size_filter = rule_size or ("Large" if kind == "large_approved" else None)
    if size_filter and (app_size or "").lower() != size_filter.lower():
        return None
    if kind == "nma_filed":
        return start_date if role == "nma" else None
    if kind == "nma_approved":
        return decided_date if (role == "nma" and app_state in lead_states) else None
    if kind == "large_approved":
        return decided_date if app_state in lead_states else None
    if kind == "discharge_decided":
        return decided_date if role == "discharge" else None
    return None


def generate(conn, touched_uids: set[str], lead_states: list[str]) -> int:
    """Insert due alerts for starred projects touched by this run.
    Returns how many alerts were inserted."""
    if not touched_uids or not _tables_exist(conn):
        return 0

    touched_projects = [r[0] for r in conn.execute(
        "SELECT DISTINCT project_id FROM project_applications WHERE uid = ANY(%s)",
        (list(touched_uids),))]
    if not touched_projects:
        return 0

    stars = conn.execute(
        "SELECT user_id, project_id FROM ui_project_stars WHERE project_id = ANY(%s)",
        (touched_projects,)).fetchall()
    if not stars:
        return 0

    user_ids = list({s[0] for s in stars})
    rules = conn.execute(
        "SELECT id, user_id, kind, after_days, app_size FROM ui_alert_rules "
        "WHERE enabled AND user_id = ANY(%s)", (user_ids,)).fetchall()

    rules_by_user: dict = {}
    for rule_id, user_id, kind, after_days, rule_size in rules:
        if kind in _LABELS:
            rules_by_user.setdefault(user_id, []).append(
                (rule_id, kind, after_days, rule_size))
    rules_by_project: dict[int, dict] = {}
    for user_id, project_id in stars:
        if user_id in rules_by_user:
            rules_by_project.setdefault(project_id, {})[user_id] = rules_by_user[user_id]

    today = date.today()
    recent_since = today - timedelta(days=RECENT_DAYS)
    inserted = 0
    for project_id, user_rules in rules_by_project.items():
        members = conn.execute(
            "SELECT pa.uid, pa.role, a.reference, a.address, a.app_size, "
            "       a.app_state, a.start_date, a.decided_date "
            "FROM project_applications pa JOIN applications a ON a.uid = pa.uid "
            "WHERE pa.project_id = %s", (project_id,)).fetchall()
        for uid, role, reference, address, app_size, app_state, start_date, decided_date in members:
            for user_id, user_rule_list in user_rules.items():
                for rule_id, kind, after_days, rule_size in user_rule_list:
                    event = _trigger_date(kind, role, app_state, app_size,
                                          start_date, decided_date,
                                          rule_size, lead_states)
                    if event is None or not (recent_since <= event <= today):
                        continue
                    message = (f"{_LABELS[kind]}: {reference or uid}"
                               + (f" — {address[:80]}" if address else ""))
                    cur = conn.execute(
                        "INSERT INTO ui_alerts (user_id, project_id, uid, rule_id, "
                        "kind, due_on, message) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (user_id, rule_id, uid) DO NOTHING",
                        (user_id, project_id, uid, rule_id, kind,
                         event + timedelta(days=after_days), message))
                    inserted += cur.rowcount
    if inserted:
        log.info("Alerts: inserted %d", inserted)
    return inserted
