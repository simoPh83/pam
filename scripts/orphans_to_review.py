"""Move unlinked orphan applications out of `projects` into the review queue.

Design change (2026-10-10): an application with no resolvable parent ref and
no scheme to attach to is NOT a project — it's a pending `grouping_review`
row keyed by address. This script retires the provisional 1-app projects the
regroup created for them and re-keys their review rows by address.

Idempotent. Run with the worker stopped. Dry-run by default; --apply commits.
See docs/2026-10-10-scheme-grouping-spec.md.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from pam.config import load_dotenv

log = logging.getLogger("orphans_to_review")

DDL = """
ALTER TABLE grouping_review ALTER COLUMN project_id DROP NOT NULL;
ALTER TABLE grouping_review ADD COLUMN IF NOT EXISTS address_key TEXT;
CREATE INDEX IF NOT EXISTS idx_grouping_review_pending
    ON grouping_review (status, reason);
CREATE INDEX IF NOT EXISTS idx_grouping_review_address
    ON grouping_review (authority, address_key);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not configured")

    conn = psycopg.connect(url, options="-c statement_timeout=300000")
    stats = Counter()
    try:
        with conn.cursor() as cur:
            for stmt in DDL.split(";"):
                if stmt.strip():
                    cur.execute(stmt)

            # Orphan review rows whose project is a 1-app placeholder.
            # Re-key by the orphan's address; candidates = schemes sharing it.
            cur.execute("""
                SELECT g.id, g.project_id, g.uid, g.reason, p.authority,
                       p.address_key
                FROM grouping_review g
                JOIN projects p ON p.id = g.project_id
                WHERE g.status = 'pending' AND g.uid IS NOT NULL
                  AND g.reason LIKE 'unlinked%' AND p.n_applications = 1
            """)
            rows = cur.fetchall()
            log.info("Orphan placeholder projects to retire: %s", len(rows))

            for gid, pid, uid, reason, auth, addr_key in rows:
                # candidate schemes = other projects at this address
                cur.execute("""
                    SELECT p2.id, p2.root_uid, p2.grouping_state,
                           p2.n_applications, a.description, a.app_type,
                           a.start_date
                    FROM projects p2
                    LEFT JOIN applications a ON a.uid = p2.root_uid
                    WHERE p2.authority = %s AND p2.address_key = %s
                      AND p2.id <> %s
                    ORDER BY p2.n_applications DESC
                """, (auth, addr_key, pid))
                cands = [
                    {"project_id": r[0], "uid": r[1], "grouping_state": r[2],
                     "n_applications": r[3], "description": (r[4] or "")[:200],
                     "app_type": r[5],
                     "start_date": str(r[6]) if r[6] else None}
                    for r in cur.fetchall()
                ]
                cur.execute("""
                    UPDATE grouping_review
                    SET project_id = NULL, address_key = %s,
                        candidates = %s::jsonb,
                        reason = %s
                    WHERE id = %s
                """, (addr_key, json.dumps(cands),
                      # unlinked_no_scheme with no same-address scheme stays;
                      # multi_scheme keeps its reason (candidates may be empty
                      # for the 353 cross-address edge cases -> no_scheme)
                      reason if cands else "unlinked_no_scheme", gid))
                # remove the membership + placeholder project
                cur.execute(
                    "DELETE FROM project_applications "
                    "WHERE project_id = %s AND uid = %s", (pid, uid))
                cur.execute("DELETE FROM projects WHERE id = %s", (pid,))
                stats[f"retired_{reason}"] += 1

            stats["projects_after"] = cur.execute(
                "SELECT count(*) FROM projects").fetchone()[0]
            cur.execute("SELECT reason, count(*) FROM grouping_review "
                        "WHERE status='pending' GROUP BY 1")
            stats.update({f"queue_{k}": v for k, v in cur.fetchall()})
            cur.execute("SELECT count(*) FROM grouping_review "
                        "WHERE status='pending' AND project_id IS NULL")
            stats["queue_address_keyed"] = cur.fetchone()[0]
    finally:
        if args.apply:
            conn.commit()
            log.info("APPLIED")
        else:
            conn.rollback()
            log.info("DRY RUN — rolled back")
        conn.close()

    print(json.dumps(dict(stats), indent=2, default=str))


if __name__ == "__main__":
    main()
