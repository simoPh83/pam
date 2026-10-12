"""One-off backfill: every ambiguous project gets exactly one pending
multi_root review row; every pending multi_root row belongs to a currently
ambiguous project.

- Stale rows (project no longer ambiguous) are resolved 'obsolete' set-based.
- Missing rows are created by re-deriving root/state via
  projects._set_root_and_state, which upserts the review row with a fresh
  candidate list (the same path the worker now maintains incrementally).

Idempotent. Dry-run by default (rolls back); --apply commits. Safe with the
worker stopped; see docs/2026-10-12-multi-root-split-backend-spec.md Part 1.

  python scripts/backfill_multi_root_reviews.py          # dry-run
  python scripts/backfill_multi_root_reviews.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from pam.config import load_dotenv
from pam.projects import _set_root_and_state

log = logging.getLogger("backfill_multi_root_reviews")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not configured")

    conn = psycopg.connect(url, options="-c statement_timeout=600000")
    stats = {}
    try:
        with conn.cursor() as cur:
            # pending multi_root rows whose project left ambiguous
            cur.execute("""
                UPDATE grouping_review r SET status = 'resolved',
                    resolution = 'obsolete', resolved_at = now()
                FROM projects p
                WHERE r.project_id = p.id AND r.reason = 'multi_root'
                  AND r.status = 'pending' AND p.grouping_state <> 'ambiguous'
            """)
            stats["stale_resolved"] = cur.rowcount

            # ambiguous projects without a pending review row
            cur.execute("""
                SELECT p.id FROM projects p
                WHERE p.grouping_state = 'ambiguous'
                  AND NOT EXISTS (SELECT 1 FROM grouping_review r
                                  WHERE r.reason = 'multi_root'
                                    AND r.status = 'pending'
                                    AND r.project_id = p.id)
                ORDER BY p.id
            """)
            missing = [r[0] for r in cur.fetchall()]
            stats["ambiguous_without_review"] = len(missing)

        for i, pid in enumerate(missing):
            _set_root_and_state(conn, pid)  # refreshes/inserts the review row
            if (i + 1) % 200 == 0:
                log.info("Re-derived %s/%s projects", i + 1, len(missing))

        with conn.cursor() as cur:
            # acceptance check (both must be 0)
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM projects p
                     WHERE p.grouping_state='ambiguous'
                       AND NOT EXISTS (SELECT 1 FROM grouping_review r
                         WHERE r.reason='multi_root' AND r.status='pending'
                           AND r.project_id=p.id)) AS ambiguous_without_review,
                  (SELECT count(*) FROM grouping_review r JOIN projects p
                     ON p.id=r.project_id
                     WHERE r.reason='multi_root' AND r.status='pending'
                       AND p.grouping_state<>'ambiguous') AS stale_reviews
            """)
            stats["check_ambiguous_without_review"], \
                stats["check_stale_reviews"] = cur.fetchone()
    finally:
        if args.apply:
            conn.commit()
            log.info("APPLIED")
        else:
            conn.rollback()
            log.info("DRY RUN — rolled back")
        conn.close()

    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
