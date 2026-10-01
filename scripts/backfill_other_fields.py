"""One-time migration: parse other_fields_json into the promoted columns.

Idempotent — only fills columns that are currently NULL, so it can be re-run
safely after new fetches if any rows slip through. Uses the same junk-filtering
and parsing rules as pam.transform.

Run:  .venv\\Scripts\\python.exe scripts\\backfill_other_fields.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pam.config import DEFAULT_CONFIG_PATH  # noqa: E402
from pam.ledger import Ledger  # noqa: E402  (ensures new columns exist)
from pam.transform import (  # noqa: E402
    _OTHER_DATE_KEYS,
    _OTHER_INT_KEYS,
    _clean,
    _date_or_none,
    _int_or_none,
)

TEXT_KEYS = ("decision", "decided_by", "source_status", "ward", "parish",
             "development_type", "planning_portal_id", "uprn",
             "appeal_reference", "appeal_result")
URL_KEYS = ("source_url", "comment_url", "map_url")

# column -> other_fields key (where they differ)
KEY_MAP = {"source_status": "status", "ward": "ward_name"}


def extract(other: dict) -> dict:
    row: dict = {}
    for col in TEXT_KEYS:
        row[col] = _clean(other.get(KEY_MAP.get(col, col)))
    for col in URL_KEYS:
        row[col] = other.get(col)
    for col in _OTHER_DATE_KEYS:
        d = _date_or_none(other.get(col))
        row[col] = d.isoformat() if d else None
    for col in _OTHER_INT_KEYS:
        row[col] = _int_or_none(other.get(col))
    return row


def main() -> None:
    import yaml
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    db_path = DEFAULT_CONFIG_PATH.parent / raw.get("output", {}).get(
        "ledger_path", "data/ledger.sqlite")

    ledger = Ledger(db_path)  # applies EXTRA_COLUMNS migrations
    conn = ledger.conn
    conn.row_factory = sqlite3.Row

    import json
    rows = conn.execute(
        "SELECT uid, other_fields_json FROM applications "
        "WHERE other_fields_json IS NOT NULL AND other_fields_json != '{}'"
    ).fetchall()

    updated = skipped = 0
    for rec in rows:
        try:
            other = json.loads(rec["other_fields_json"])
        except (TypeError, json.JSONDecodeError):
            skipped += 1
            continue
        values = extract(other)
        set_sql = ", ".join(
            f"{col} = COALESCE({col}, ?)" for col in values
        )
        conn.execute(
            f"UPDATE applications SET {set_sql} WHERE uid = ?",
            (*values.values(), rec["uid"]),
        )
        updated += 1
        if updated % 2000 == 0:
            conn.commit()
            print(f"  ...{updated} rows")
    conn.commit()
    print(f"Backfill complete: {updated} rows updated, {skipped} skipped (bad JSON)")
    ledger.close()


if __name__ == "__main__":
    main()
