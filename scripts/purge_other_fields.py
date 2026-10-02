"""One-time migration: shrink applications.other_fields_json to extras only.

Removes keys whose value is already stored in a dedicated column (same rules as
pam.transform.extra_fields), leaving only data the schema does not capture.
Idempotent. Backs up the DB first (unless --no-backup) and VACUUMs afterwards.

Run:  .venv\\Scripts\\python.exe scripts\\purge_other_fields.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pam.config import load_config  # noqa: E402
from pam.transform import extra_fields  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--db", type=Path)
    args = parser.parse_args()

    db_path = args.db or load_config(argv=[]).ledger_path
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if not args.dry_run and not args.no_backup:
        backup = db_path.with_suffix(".sqlite.bak")
        shutil.copyfile(db_path, backup)
        print(f"Backup written to {backup}")

    before = after = changed = 0
    dropped: Counter = Counter()
    updates = []
    for rec in conn.execute("SELECT * FROM applications "
                            "WHERE other_fields_json IS NOT NULL"):
        original = rec["other_fields_json"]
        other = json.loads(original or "{}")
        extras = extra_fields(other, dict(rec))
        new = json.dumps(extras, ensure_ascii=False)
        before += len(original)
        after += len(new)
        if new != original:
            changed += 1
            dropped.update(set(other) - set(extras))
            updates.append((new, rec["uid"]))

    print(f"{changed} rows change; JSON {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB")
    for key, n in dropped.most_common():
        print(f"  dropped {key}: {n}")

    if args.dry_run:
        return 0
    conn.executemany("UPDATE applications SET other_fields_json = ? WHERE uid = ?",
                     updates)
    conn.commit()
    conn.execute("VACUUM")
    print(f"Done. DB size now {db_path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
