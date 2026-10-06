"""Ledger (Postgres): every application we've ever seen, with its state.

The ledger is the memory of the system. It tracks all watched states
(e.g. Undecided) so that a later transition to a lead state (Permitted /
Conditions) is detected as an event — the outreach trigger.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from pam.transform import extra_fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    uid            TEXT PRIMARY KEY,
    reference      TEXT,
    authority      TEXT,
    app_state      TEXT,
    app_size       TEXT,
    start_date     DATE,
    decided_date   DATE,
    first_seen     DATE NOT NULL,
    last_updated   DATE NOT NULL,
    in_leads_sheet BOOLEAN NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS fetch_progress (
    run_window   TEXT NOT NULL,
    area         TEXT NOT NULL,
    state        TEXT NOT NULL,
    last_offset  INTEGER NOT NULL,
    done         BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (run_window, area, state)
);
-- One row per observed app_state change. First sightings are not recorded:
-- applications.first_seen already carries that information.
CREATE TABLE IF NOT EXISTS state_history (
    id             BIGSERIAL PRIMARY KEY,
    uid            TEXT NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL,
    old_state      TEXT,
    new_state      TEXT,
    decided_date   DATE,
    decision       TEXT,
    last_different TIMESTAMPTZ
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
    "permission_expires": "DATE",
    "distance_km": "DOUBLE PRECISION",
    "lat": "DOUBLE PRECISION",
    "lng": "DOUBLE PRECISION",
    "council_url": "TEXT",
    "docs_url": "TEXT",
    "other_fields_json": "JSONB",
    # Promoted from other_fields (schema review 2026-10-01)
    "decision": "TEXT",
    "decided_by": "TEXT",
    "source_status": "TEXT",
    "ward": "TEXT",
    "parish": "TEXT",
    "development_type": "TEXT",
    "comment_url": "TEXT",
    "map_url": "TEXT",
    "planning_portal_id": "TEXT",
    "uprn": "TEXT",
    "appeal_reference": "TEXT",
    "appeal_result": "TEXT",
    "date_received": "DATE",
    "date_validated": "DATE",
    "target_decision_date": "DATE",
    "consultation_end_date": "DATE",
    "application_expires_date": "DATE",
    "decision_issued_date": "DATE",
    "appeal_date": "DATE",
    "appeal_decision_date": "DATE",
    "n_documents": "INTEGER",
    "n_comments": "INTEGER",
    "n_constraints": "INTEGER",
    "n_dwellings": "INTEGER",
    "n_statutory_days": "INTEGER",
    "application_type": "TEXT",
    "applicant_address": "TEXT",
    "comment_date": "DATE",
    "neighbour_consultation_start_date": "DATE",
    "neighbour_consultation_end_date": "DATE",
    "consultation_start_date": "DATE",
    "decision_published_date": "DATE",
    "last_changed": "TIMESTAMPTZ",
    "last_different": "TIMESTAMPTZ",
    "last_scraped": "TIMESTAMPTZ",
}


# URL columns stored as a tail after a per-authority base (table authority_urls).
# source_url is the same portal page for every row, so only the base is kept.
URL_FIELDS = ("council_url", "docs_url", "comment_url", "map_url")
AUTHORITY_URLS_DDL = (
    "CREATE TABLE IF NOT EXISTS authority_urls ("
    "authority TEXT NOT NULL, field TEXT NOT NULL, base_url TEXT NOT NULL, "
    "PRIMARY KEY (authority, field))"
)


def url_base(url: str) -> str:
    """Prefix up to and including the last '/' of the path (query string excluded)."""
    return url[: url.split("?", 1)[0].rfind("/") + 1]


def _full_urls_view_sql(replace: str) -> str:
    cols, joins = [], []
    for i, field in enumerate(URL_FIELDS):
        cols.append(
            f"CASE WHEN a.{field} IS NULL OR a.{field} LIKE 'http%' OR b{i}.base_url IS NULL "
            f"THEN a.{field} ELSE b{i}.base_url || a.{field} END AS {field}")
        joins.append(f"LEFT JOIN authority_urls b{i} "
                     f"ON b{i}.authority = a.authority AND b{i}.field = '{field}'")
    cols.append("s.base_url AS source_url")
    joins.append("LEFT JOIN authority_urls s ON s.authority = a.authority "
                 "AND s.field = 'source_url'")
    return (f"{replace} VIEW applications_full AS SELECT a.uid, {', '.join(cols)} "
            f"FROM applications a {' '.join(joins)}")


class Ledger:
    """`target` is a postgres:// connection URL."""

    def __init__(self, target):
        if not str(target).startswith(("postgres://", "postgresql://")):
            raise SystemExit("A postgres:// DATABASE_URL is required (SQLite is no longer supported)")
        import psycopg
        self.conn = psycopg.connect(str(target))
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                self.conn.execute(stmt)
        for col, coltype in EXTRA_COLUMNS.items():
            self.conn.execute(
                f"ALTER TABLE applications ADD COLUMN IF NOT EXISTS {col} {coltype}")
        self.conn.execute(AUTHORITY_URLS_DDL)
        from pam.projects import ensure_schema
        ensure_schema(self.conn)
        self.conn.execute(_full_urls_view_sql("CREATE OR REPLACE"))
        self._bases = {(a, f): b for a, f, b in self._exec(
            "SELECT authority, field, base_url FROM authority_urls").fetchall()}
        self.conn.commit()

    def _exec(self, sql: str, params=()):
        """Run SQL written with ? placeholders (converted to psycopg's %s)."""
        return self.conn.execute(sql.replace("?", "%s"), params)

    def _set_base(self, authority: str, field: str, base: str) -> None:
        self._exec(
            "INSERT INTO authority_urls (authority, field, base_url) VALUES (?, ?, ?) "
            "ON CONFLICT (authority, field) DO NOTHING", (authority, field, base))
        self._bases[(authority, field)] = base

    def _shorten(self, authority: str, field: str, url: str | None) -> str | None:
        """Strip the authority's base from url; URLs on another base stay absolute."""
        if not url:
            return url
        base = self._bases.get((authority, field))
        if base is None:
            base = url_base(url)
            self._set_base(authority, field, base)
        return url[len(base):] if url.startswith(base) else url

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
            (window, area, state, offset, bool(done)),
        )
        self.conn.commit()

    def in_leads_sheet(self, uid: str) -> bool:
        row = self._exec(
            "SELECT in_leads_sheet FROM applications WHERE uid = ?", (uid,)
        ).fetchone()
        return bool(row[0]) if row else False

    def upsert(self, row: dict, *, in_leads: bool, raw: dict | None = None) -> None:
        today = date.today()

        def d(key):
            value = row.get(key)
            if key in URL_FIELDS:
                return self._shorten(row["authority"], key, value)
            return value or None if isinstance(value, str) else value

        if row.get("source_url") and (row["authority"], "source_url") not in self._bases:
            self._set_base(row["authority"], "source_url", row["source_url"])

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
            "development_type", "comment_url", "map_url",
            "planning_portal_id", "uprn", "appeal_reference", "appeal_result",
            "date_received", "date_validated", "target_decision_date",
            "consultation_end_date", "application_expires_date",
            "decision_issued_date", "appeal_date", "appeal_decision_date",
            "n_documents", "n_comments", "n_constraints", "n_dwellings",
            "n_statutory_days", "application_type", "applicant_address",
            "comment_date", "neighbour_consultation_start_date",
            "neighbour_consultation_end_date", "consultation_start_date",
            "decision_published_date",
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
        # Record genuine state changes only; a first sighting adds nothing
        # beyond applications.first_seen and cost 292k rows / ~54 MB.
        if prev_state is not None and new_state and new_state != prev_state:
            self._exec(
                "INSERT INTO state_history (uid, observed_at, old_state, new_state, "
                "decided_date, decision, last_different) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["uid"], datetime.now(timezone.utc), prev_state,
                 new_state, d("decided_date"), d("decision"), d("last_different")),
            )
        update_sql = ",\n                ".join(
            f"{c} = COALESCE(excluded.{c}, applications.{c})" for c in mutable
        )
        # last_updated bumps only when a tracked field actually changed (or the
        # state flipped) — not on every nightly re-fetch.
        change_checks = " OR ".join(
            f"excluded.{c} IS DISTINCT FROM applications.{c}" for c in mutable
            if c != "other_fields_json"
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
                last_updated   = CASE WHEN {change_checks}
                                      THEN excluded.last_updated
                                      ELSE applications.last_updated END,
                in_leads_sheet = (applications.in_leads_sheet OR excluded.in_leads_sheet)
            """,
            (*values, today, today, bool(in_leads)),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
