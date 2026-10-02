"""One-off: split agent info into agent_company / agent_name / agent_address / agent_display.

    python scripts/migrate_agent_columns.py [--target data/ledger.sqlite | postgres-url]
                                            [--dry-run]

Without --target, uses DATABASE_URL from .env if set, else data/ledger.sqlite.

Legacy rows hold one `agent_name` that mostly came from PlanIt's `agent_company`
(the original raw keys were already purged from other_fields_json), so it is moved
to agent_company. A later re-fetch overwrites all agent_* columns from the source.
agent_address is lifted out of other_fields_json. Safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pam.config import load_dotenv  # noqa: E402
from pam.ledger import Ledger  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    target = args.target or os.environ.get("DATABASE_URL") or str(ROOT / "data" / "ledger.sqlite")
    ledger = Ledger(target)  # adds the new columns if missing

    moved = ledger._exec(
        "SELECT COUNT(*) FROM applications "
        "WHERE agent_display IS NULL AND agent_name IS NOT NULL").fetchone()[0]
    rows = ledger._exec(
        "SELECT uid, other_fields_json FROM applications "
        "WHERE other_fields_json LIKE ?", ("%agent_address%",)).fetchall()
    print(f"{moved} legacy agent names -> agent_company; {len(rows)} rows with agent_address")
    if args.dry_run:
        return

    ledger._exec(
        "UPDATE applications SET agent_company = agent_name, agent_name = NULL "
        "WHERE agent_display IS NULL AND agent_name IS NOT NULL")

    updates = []
    for uid, blob in rows:
        extras = json.loads(blob)
        address = extras.pop("agent_address", None)
        address = str(address).strip() if address else None
        if address and address.lower() in {"see source", "n/a", "na", "-", "unknown"}:
            address = None
        updates.append((address, json.dumps(extras, ensure_ascii=False), uid))
    sql = "UPDATE applications SET agent_address = ?, other_fields_json = ? WHERE uid = ?"
    if ledger.pg:
        with ledger.conn.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), updates)
    else:
        ledger.conn.executemany(sql, updates)

    ledger._exec("UPDATE applications SET agent_display = COALESCE(agent_company, agent_name) "
                 "WHERE agent_display IS NULL")
    ledger.conn.commit()
    total, with_agent, with_addr = ledger._exec(
        "SELECT COUNT(*), COUNT(agent_display), COUNT(agent_address) FROM applications"
    ).fetchone()
    print(f"done: {total} rows, {with_agent} with agent_display, {with_addr} with agent_address")
    ledger.close()


if __name__ == "__main__":
    main()
