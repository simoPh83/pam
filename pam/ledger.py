"""Ledger (SQLite locally, Postgres when a postgres:// URL is given): every application we've ever seen, with its state.

The ledger is the memory of the system. It tracks all watched states
(e.g. Undecided) so that a later transition to a lead state (Permitted /
Conditions) is detected as an event — the outreach trigger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from pam.transform import extra_fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    uid            TEXT PRIMARY KEY,
    reference      TEXT,
    authority      TEXT,
    app_state      TEXT,
    app_size       TEXT,
    start_date     TEXT,
    decided_date   TEXT,
    first_seen     TEXT NOT NULL,
    last_updated   TEXT NOT NULL,
    in_leads_sheet INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS fetch_progress (
    run_window   TEXT NOT NULL,
    area         TEXT NOT NULL,
    state        TEXT NOT NULL,
    last_offset  INTEGER NOT NULL,
    done         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_window, area, state)
);
-- One row per observed app_state change (and the first sighting, old_state NULL).
CREATE TABLE IF NOT EXISTS state_history (
    id             __IDCOL__,
    uid            TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    old_state      TEXT,
    new_state      TEXT,
    decided_date   TEXT,
    decision       TEXT,
    last_different TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_history_uid ON state_history (uid);
"""

# Columns added after the initial release — migrated idempotently.
EXTRA_COLUMNS = {
    "address": "TEXT",
    "postcode": "TEXT",
    "description": "TEXT",
    "app_type": "TEXT",
    "agent_name": "TEXT",
    "agent_company": "TEXT",
    "agent_address": "TEXT",
    "agent_display": "TEXT",
    "applicant_name": "TEXT",
    "permission_expires": "TEXT",
    "distance_km": "REAL",
    "lat": "REAL",
    "lng": "REAL",
    "council_url": "TEXT",
    "docs_url": "TEXT",
    "other_fields_json": "TEXT",
    # Promoted from other_fields (schema review 2026-10-01)
    "decision": "TEXT",
    "decided_by": "TEXT",
    "source_status": "TEXT",
    "ward": "TEXT",
    "parish": "TEXT",
    "development_type": "TEXT",
    "source_url": "TEXT",
    "comment_url": "TEXT",
    "map_url": "TEXT",
    "planning_portal_id": "TEXT",
    "uprn": "TEXT",
    "appeal_reference": "TEXT",
    "appeal_result": "TEXT",
    "date_received": "TEXT",
    "date_validated": "TEXT",
    "target_decision_date": "TEXT",
    "consultation_end_date": "TEXT",
    "application_expires_date": "TEXT",
    "decision_issued_date": "TEXT",
    "appeal_date": "TEXT",
    "appeal_decision_date": "TEXT",
    "n_documents": "INTEGER",
    "n_comments": "INTEGER",
    "n_constraints": "INTEGER",
    "n_dwellings": "INTEGER",
    "last_changed": "TEXT",
    "last_different": "TEXT",
    "last_scraped": "TEXT",
}


def _is_postgres(target) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


class Ledger:
    """`target` is a SQLite file path or a postgres:// connection URL."""

    def __init__(self, target):
        self.pg = _is_postgres(target)
        if self.pg:
            import psycopg
            self.conn = psycopg.connect(str(target))
            schema = SCHEMA.replace("__IDCOL__", "BIGSERIAL PRIMARY KEY")
            for stmt in schema.split(";"):
                if stmt.strip():
                    self.conn.execute(stmt)
            for col, coltype in EXTRA_COLUMNS.items():
                self.conn.execute(
                    f"ALTER TABLE applications ADD COLUMN IF NOT EXISTS {col} {coltype}")
        else:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
            self.conn.executescript(
                SCHEMA.replace("__IDCOL__", "INTEGER PRIMARY KEY AUTOINCREMENT"))
            existing = {row[1] for row in
                        self.conn.execute("PRAGMA table_info(applications)")}
            for col, coltype in EXTRA_COLUMNS.items():
                if col not in existing:
                    self.conn.execute(
                        f"ALTER TABLE applications ADD COLUMN {col} {coltype}")
        # Seed history for rows stored before it existed (first sighting only)
        if not self._exec("SELECT 1 FROM state_history LIMIT 1").fetchone():
            self._exec(
                "INSERT INTO state_history (uid, observed_at, old_state, new_state, "
                "decided_date, decision) "
                "SELECT uid, first_seen, NULL, app_state, decided_date, decision "
                "FROM applications")
        self.conn.commit()

    def _exec(self, sql: str, params=()):
        """Run SQL written with ? placeholders on either backend."""
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.conn.execute(sql, params)

    def get_state(self, uid: str) -> str | None:
        row = self._exec(
            "SELECT app_state FROM applications WHERE uid = ?", (uid,)
        ).fetchone()
        return row[0] if row else None
    # --- fetch progress (resumable backfills) ---

    def progress(self, window: str, area: str, state: str) -> tuple[int, bool]:
        row = self._exec(
            "SELECT last_offset, done FROM fetch_progress "
            "WHERE run_window = ? AND area = ? AND state = ?",
            (window, area, state),
        ).fetchone()
        return (row[0], bool(row[1])) if row else (0, False)

    def save_progress(self, window: str, area: str, state: str,
                      offset: int, done: bool) -> None:
        self._exec(
            "INSERT INTO fetch_progress (run_window, area, state, last_offset, done) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_window, area, state) DO UPDATE SET "
            "last_offset = excluded.last_offset, done = excluded.done",
            (window, area, state, offset, int(done)),
        )
        self.conn.commit()

    def in_leads_sheet(self, uid: str) -> bool:
        row = self._exec(
            "SELECT in_leads_sheet FROM applications WHERE uid = ?", (uid,)
        ).fetchone()
        return bool(row[0]) if row else False

    def upsert(self, row: dict, *, in_leads: bool, raw: dict | None = None) -> None:
        today = date.today().isoformat()

        def d(key):
            value = row.get(key)
            return value.isoformat() if isinstance(value, date) else value

        columns = (
            "uid", "reference", "authority", "app_state", "app_size", "app_type",
            "address", "postcode", "description",
            "agent_name", "agent_company", "agent_address", "agent_display",
            "applicant_name",
            "start_date", "decided_date", "permission_expires",
            "distance_km", "lat", "lng", "council_url", "docs_url",
            "other_fields_json",
            # promoted from other_fields
            "decision", "decided_by", "source_status", "ward", "parish",
            "development_type", "source_url", "comment_url", "map_url",
            "planning_portal_id", "uprn", "appeal_reference", "appeal_result",
            "date_received", "date_validated", "target_decision_date",
            "consultation_end_date", "application_expires_date",
            "decision_issued_date", "appeal_date", "appeal_decision_date",
            "n_documents", "n_comments", "n_constraints", "n_dwellings",
            "last_changed", "last_different", "last_scraped",
        )
        values = [d(key) for key in columns if key != "other_fields_json"]
        values.insert(columns.index("other_fields_json"),
                      json.dumps(extra_fields(raw.get("other_fields"), row),
                                 ensure_ascii=False)
                      if raw else None)

        mutable = [c for c in columns
                   if c not in ("uid", "agent_name", "agent_company", "agent_address",
                                  "agent_display", "applicant_name")]
        prev_state = self.get_state(row["uid"])
        new_state = row.get("app_state")
        if prev_state is None or (new_state and new_state != prev_state):
            self._exec(
                "INSERT INTO state_history (uid, observed_at, old_state, new_state, "
                "decided_date, decision, last_different) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["uid"], datetime.now().isoformat(timespec="seconds"), prev_state,
                 new_state, d("decided_date"), d("decision"), d("last_different")),
            )
        greatest = "GREATEST" if self.pg else "MAX"
        update_sql = ",\n                ".join(
            f"{c} = COALESCE(excluded.{c}, applications.{c})" for c in mutable
        )
        self._exec(
            f"""
            INSERT INTO applications
                ({", ".join(columns)}, first_seen, last_updated, in_leads_sheet)
            VALUES ({", ".join("?" * (len(columns) + 3))})
            ON CONFLICT(uid) DO UPDATE SET
                {update_sql},
                agent_name     = excluded.agent_name,
                agent_company  = excluded.agent_company,
                agent_address  = excluded.agent_address,
                agent_display  = excluded.agent_display,
                applicant_name = COALESCE(excluded.applicant_name, applications.applicant_name),
                last_updated   = excluded.last_updated,
                in_leads_sheet = {greatest}(applications.in_leads_sheet, excluded.in_leads_sheet)
            """,
            (*values, today, today, int(in_leads)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
