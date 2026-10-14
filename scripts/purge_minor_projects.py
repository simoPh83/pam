"""Retroactive minor-works purge — dry-run by default.

    .venv\\Scripts\\python.exe scripts\\purge_minor_projects.py            # dry run
    .venv\\Scripts\\python.exe scripts\\purge_minor_projects.py --apply    # execute

Classifies every project by its MAIN application (earliest role='original'
member) via pam.classify and purges the minors via pam.purge, writing
tombstones (dismissed_projects / dismissed_refs) so purged schemes are never
re-ingested. Protected: projects with ui_project_stars / ui_project_meta /
ui_project_practices rows. Postponed (never purged): projects with no
original-role member — their main application was never fetched.

Deploy sequence (per playbook): stop the worker, run --apply, re-run the
regroup scripts if needed, restart the worker.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from collections import Counter
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pam import purge  # noqa: E402
from pam.config import load_dotenv  # noqa: E402
from pam.dismissals import Dismissals, ensure_schema  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="execute the purge (default: dry run)")
    args = ap.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    load_dotenv(Path(".env"))
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    ensure_schema(conn)

    # Half-purged projects from an interrupted earlier run (tombstoned but
    # project rows remain) are retired first.
    zombies = purge.cleanup_zombie_projects(conn, dry_run=not args.apply)
    if zombies:
        print(f"  zombie (half-purged) projects retired: {zombies:,}")

    print("Classifying all projects by their main application ...")
    verdicts: dict[str, list] = {"purge": [], "keep": [], "postpone": [],
                                 "protected": []}
    reasons = {}
    for row, verdict, reason in purge.iter_verdicts(conn):
        verdicts[verdict].append(row)
        if verdict == "purge":
            reasons[row["id"]] = reason or "minor"

    n_apps = sum(r["n_applications"] for r in verdicts["purge"])
    print(f"\n  purge:     {len(verdicts['purge']):7,} projects "
          f"({n_apps:,} applications)")
    print(f"  keep:      {len(verdicts['keep']):7,}")
    print(f"  postpone:  {len(verdicts['postpone']):7,}  (main not in DB)")
    print(f"  protected: {len(verdicts['protected']):7,}  (user-curated)")
    print("\n  purge reasons (classifier buckets):")
    for reason, n in Counter(r for r in reasons.values()).most_common():
        print(f"    {n:7,}  {reason}")

    random.seed(7)
    print("\n  purge samples (15 random):")
    for r in random.sample(verdicts["purge"], min(15, len(verdicts["purge"]))):
        print(f"    [{reasons[r['id']]}] ({r['authority']}) "
              f"{(r['main_desc'] or '')[:110]!r}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to execute.")
        return

    dismissed = Dismissals.load(conn)
    stats = purge.purge_projects(
        conn, dismissed, [r["id"] for r in verdicts["purge"]], reasons)
    print(f"\nPURGED: {dict(stats)}")
    print("Post-purge size check: "
          f"{conn.execute('SELECT pg_size_pretty(pg_database_size(current_database()))').fetchone()[0]}")


if __name__ == "__main__":
    main()
