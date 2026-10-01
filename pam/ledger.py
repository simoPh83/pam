"""SQLite ledger: every application we've ever seen, with its state.

The ledger is the memory of the system. It tracks all watched states
(e.g. Undecided) so that a later transition to a lead state (Permitted /
Conditions) is detected as an event — the outreach trigger.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

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
"""

# Columns added after the initial release — migrated idempotently.
EXTRA_COLUMNS = {
    "address": "TEXT",
    "postcode": "TEXT",
    "description": "TEXT",
    "app_type": "TEXT",
    "agent_name": "TEXT",
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
}


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(applications)")}
        for col, coltype in EXTRA_COLUMNS.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {coltype}")
        self.conn.commit()

    def get_state(self, uid: str) -> str | None:
        row = self.conn.execute(
            "SELECT app_state FROM applications WHERE uid = ?", (uid,)
        ).fetchone()
        return row[0] if row else None

    # --- fetch progress (resumable backfills) ---

    def progress(self, window: str, area: str, state: str) -> tuple[int, bool]:
        row = self.conn.execute(
            "SELECT last_offset, done FROM fetch_progress "
            "WHERE run_window = ? AND area = ? AND state = ?",
            (window, area, state),
        ).fetchone()
        return (row[0], bool(row[1])) if row else (0, False)

    def save_progress(self, window: str, area: str, state: str,
                      offset: int, done: bool) -> None:
        self.conn.execute(
            "INSERT INTO fetch_progress (run_window, area, state, last_offset, done) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_window, area, state) DO UPDATE SET "
            "last_offset = excluded.last_offset, done = excluded.done",
            (window, area, state, offset, int(done)),
        )
        self.conn.commit()

    def in_leads_sheet(self, uid: str) -> bool:
        row = self.conn.execute(
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
            "agent_name", "applicant_name",
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
        )
        values = [d(key) for key in columns if key != "other_fields_json"]
        values.insert(columns.index("other_fields_json"),
                      json.dumps(raw.get("other_fields") or {}, ensure_ascii=False)
                      if raw else None)

        mutable = [c for c in columns
                   if c not in ("uid", "agent_name", "applicant_name")]
        update_sql = ",\n                ".join(
            f"{c} = COALESCE(excluded.{c}, applications.{c})" for c in mutable
        )
        self.conn.execute(
            f"""
            INSERT INTO applications
                ({", ".join(columns)}, first_seen, last_updated, in_leads_sheet)
            VALUES ({", ".join("?" * (len(columns) + 3))})
            ON CONFLICT(uid) DO UPDATE SET
                {update_sql},
                agent_name     = COALESCE(excluded.agent_name, agent_name),
                applicant_name = COALESCE(excluded.applicant_name, applicant_name),
                last_updated   = excluded.last_updated,
                in_leads_sheet = MAX(in_leads_sheet, excluded.in_leads_sheet)
            """,
            (*values, today, today, int(in_leads)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
