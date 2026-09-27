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
        self.conn.execute(
            """
            INSERT INTO applications
                (uid, reference, authority, app_state, app_size, app_type,
                 address, postcode, description,
                 agent_name, applicant_name,
                 start_date, decided_date, permission_expires,
                 distance_km, lat, lng, council_url, docs_url,
                 other_fields_json,
                 first_seen, last_updated, in_leads_sheet)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(uid) DO UPDATE SET
                app_state      = excluded.app_state,
                decided_date   = excluded.decided_date,
                agent_name     = COALESCE(excluded.agent_name, agent_name),
                applicant_name = COALESCE(excluded.applicant_name, applicant_name),
                last_updated   = excluded.last_updated,
                in_leads_sheet = MAX(in_leads_sheet, excluded.in_leads_sheet)
            """,
            (
                row["uid"], row["reference"], row["authority"], row["app_state"],
                row["app_size"], row["app_type"],
                row["address"], row["postcode"], row["description"],
                row["agent_name"], row["applicant_name"],
                row["start_date"].isoformat() if row["start_date"] else None,
                row["decided_date"].isoformat() if row["decided_date"] else None,
                row["permission_expires"].isoformat() if row["permission_expires"] else None,
                row["distance_km"], row["lat"], row["lng"],
                row["council_url"], row["docs_url"],
                json.dumps(raw.get("other_fields") or {}, ensure_ascii=False) if raw else None,
                today, today, int(in_leads),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
