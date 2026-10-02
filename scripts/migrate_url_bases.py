"""One-off: move shared URL prefixes into authority_urls and drop applications.source_url.

    python scripts/migrate_url_bases.py [--target data/ledger.sqlite | postgres-url]
                                        [--dry-run] [--vacuum]

Without --target, uses DATABASE_URL from .env if set, else data/ledger.sqlite.
For each (authority, field) the most common base (see pam.ledger.url_base) is kept in
authority_urls and stripped from the row; URLs on another base stay absolute. Idempotent.
--vacuum (Postgres) runs VACUUM FULL afterwards to give the space back to the database.
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pam.config import load_dotenv  # noqa: E402
from pam.ledger import URL_FIELDS, Ledger, url_base  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--vacuum", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    target = args.target or os.environ.get("DATABASE_URL") or str(ROOT / "data" / "ledger.sqlite")
    ledger = Ledger(target)
    cols = {r[0] for r in ledger._exec(
        "SELECT column_name FROM information_schema.columns WHERE table_name='applications'"
        if ledger.pg else "SELECT name FROM pragma_table_info('applications')").fetchall()}

    saved = 0
    fields = list(URL_FIELDS) + (["source_url"] if "source_url" in cols else [])
    for field in fields:
        counts: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for authority, url in ledger._exec(
                f"SELECT authority, {field} FROM applications "
                f"WHERE {field} LIKE ?", ("http%",)).fetchall():
            counts[authority][url if field == "source_url" else url_base(url)] += 1
        for authority, counter in counts.items():
            base = ledger._bases.get((authority, field)) or counter.most_common(1)[0][0]
            n = len(base)
            matching = sum(c for b, c in counter.items()
                           if field != "source_url" and b.startswith(base))
            print(f"{field:12} {authority:24} {base}  ({matching} rows)")
            saved += n * matching
            if args.dry_run:
                continue
            if (authority, field) not in ledger._bases:
                ledger._set_base(authority, field, base)
            if field != "source_url":
                ledger._exec(
                    f"UPDATE applications SET {field} = SUBSTR({field}, ?) "
                    f"WHERE authority = ? AND SUBSTR({field}, 1, ?) = ?",
                    (n + 1, authority, n, base))
    print(f"approx. {saved / 1e6:.1f} MB of URL text removed (before source_url)")
    if args.dry_run:
        return

    if "source_url" in cols:
        ledger.conn.commit()
        ledger._exec("ALTER TABLE applications DROP COLUMN source_url")
        print("dropped applications.source_url")
    ledger.conn.commit()
    if args.vacuum and ledger.pg:
        ledger.conn.autocommit = True
        ledger.conn.execute("VACUUM FULL applications")
        print("vacuumed")
    ledger.close()


if __name__ == "__main__":
    main()
