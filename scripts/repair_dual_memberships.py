"""Repair dual project memberships and add the one-project-per-app invariant.

Every application must be a member of exactly one project. Incremental
group() used to insert a membership in the resolved project without
removing the uid's existing membership elsewhere (PK is (project_id, uid),
so the upsert never saw the old row). group() now moves memberships; this
script repairs the backlog and then enforces the invariant with a unique
index.

Winner rule per uid: the project containing one of its resolved reference
neighbours (refs resolve authority-wide, table-only; several winners ->
lowest id, those projects are merge candidates); else the non-singleton
project; ties -> lowest project id. Losers are deleted, missing_parents and
pending review rows follow the uid, affected projects are re-derived, and
emptied projects are retired (dependents re-pointed like the migration).
A uid in 3+ projects can need more than one pass, so the repair loops until
no duals remain (bounded at 5 passes).

Idempotent. Dry-run by default (rolls back); --apply commits.
See docs/2026-10-12-backend-issue-dual-membership.md.

  python scripts/repair_dual_memberships.py          # dry-run
  python scripts/repair_dual_memberships.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from pam.config import load_dotenv
from pam.projects import (_retire_projects, parent_refs_of, refresh)

log = logging.getLogger("repair_dual_memberships")


def _repair_pass(conn, duals, stats: Counter) -> None:
    """One repair sweep over the given dual-membered uids."""
    for i in range(0, len(duals), 200):
        batch = duals[i:i + 200]
        rows = conn.execute(
            "SELECT pa.project_id, pa.uid, a.authority, a.description, "
            "       p.n_applications "
            "FROM project_applications pa "
            "JOIN applications a ON a.uid = pa.uid "
            "JOIN projects p ON p.id = pa.project_id "
            "WHERE pa.uid = ANY(%s)", (batch,)).fetchall()
        by_uid: dict[str, list] = defaultdict(list)
        for pid, uid, auth, desc, n in rows:
            by_uid[uid].append((pid, auth, desc, n))

        # resolve refs authority-wide (same exact + suffix-stripped matching
        # as group())
        resolved: dict = {}  # (authority, cited_ref) -> uid
        for auth in {m[1] for mems in by_uid.values() for m in mems}:
            refs = {r for mems in by_uid.values() for _p, a, desc, _n in mems
                    if a == auth for r in parent_refs_of(desc, a)}
            if not refs:
                continue
            for ref, uid in conn.execute(
                    "SELECT cand.ref, a.uid "
                    "FROM unnest(%s::text[]) AS cand(ref) "
                    "JOIN applications a ON a.authority = %s "
                    "  AND (upper(a.reference) = cand.ref "
                    "       OR starts_with(upper(a.reference), "
                    "            cand.ref || '/')) "
                    "ORDER BY cand.ref, a.start_date ASC NULLS LAST",
                    (list(refs), auth)):
                resolved.setdefault((auth, ref), uid)

        moved, touched = [], set()
        for uid, mems in by_uid.items():
            pids = [m[0] for m in mems]
            auth, desc = mems[0][1], mems[0][2]
            # winner: project holding a resolved reference neighbour
            neigh = {resolved.get((auth, r))
                     for r in parent_refs_of(desc, auth)} - {None, uid}
            winners = [pid for pid, _a, _d, _n in mems if conn.execute(
                "SELECT 1 FROM project_applications WHERE project_id = %s "
                "AND uid = ANY(%s) LIMIT 1",
                (pid, sorted(neigh) or [None])).fetchone()]
            if winners:  # several: merge candidates; take the lowest id
                keep = min(winners)
                stats["by_reference"] += 1
            else:
                non_single = [m for m in mems if m[3] > 1]
                keep = (non_single[0][0] if len(non_single) == 1
                        else min(pids))
                stats["by_size" if non_single else "by_lowest_id"] += 1
            for pid in pids:
                if pid != keep:
                    moved.append((uid, pid, keep))
                    touched.update((pid, keep))

        if moved:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TEMP TABLE _fix (uid text, old_pid bigint, "
                    "keep_pid bigint) ON COMMIT DROP")
                with cur.copy("COPY _fix FROM STDIN") as cp:
                    for uid, old, keep in moved:
                        cp.write_row((uid, old, keep))
                cur.execute(
                    "UPDATE missing_parents mp SET project_id = f.keep_pid "
                    "FROM _fix f WHERE mp.requested_by = f.uid "
                    "AND mp.project_id = f.old_pid")
                cur.execute(
                    "UPDATE grouping_review g SET project_id = f.keep_pid "
                    "FROM _fix f WHERE g.uid = f.uid "
                    "AND g.project_id = f.old_pid AND g.status = 'pending'")
                cur.execute(
                    "DELETE FROM project_applications pa USING _fix f "
                    "WHERE pa.uid = f.uid AND pa.project_id = f.old_pid")
                cur.execute("DROP TABLE _fix")
            stats["memberships_deleted"] += len(moved)

        if touched:
            refresh(conn, sorted(touched))
            succ_of = {old: keep for _uid, old, keep in moved}
            stats["projects_retired"] += len(
                _retire_projects(conn, touched, succ_of))
        log.info("Batch %s-%s done (%s moved so far)",
                 i, i + len(batch), stats["memberships_deleted"])


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
    stats = Counter()
    try:
        stats["dual_uids"] = conn.execute(
            "SELECT count(*) FROM (SELECT uid FROM project_applications "
            "GROUP BY uid HAVING count(*) > 1) t").fetchone()[0]
        log.info("Dual-membership uids: %s", stats["dual_uids"])

        for rnd in range(1, 6):
            duals = [r[0] for r in conn.execute(
                "SELECT uid FROM project_applications GROUP BY uid "
                "HAVING count(*) > 1").fetchall()]
            if not duals:
                break
            log.info("Pass %s: %s dual uids", rnd, len(duals))
            stats[f"pass_{rnd}"] = len(duals)
            _repair_pass(conn, duals, stats)

        stats["duals_after"] = conn.execute(
            "SELECT count(*) FROM (SELECT uid FROM project_applications "
            "GROUP BY uid HAVING count(*) > 1) t").fetchone()[0]
        stats["empty_projects_after"] = conn.execute(
            "SELECT count(*) FROM projects p WHERE NOT EXISTS ("
            "  SELECT 1 FROM project_applications pa "
            "  WHERE pa.project_id = p.id)").fetchone()[0]

        if stats["duals_after"] == 0:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "project_applications_uid_key ON project_applications (uid)")
            stats["unique_index"] = "created"
        else:
            stats["unique_index"] = "SKIPPED - duals remain"
    finally:
        if args.apply:
            conn.commit()
            log.info("APPLIED")
        else:
            conn.rollback()
            log.info("DRY RUN - rolled back")
        conn.close()

    print(json.dumps(dict(stats), indent=2, default=str))


if __name__ == "__main__":
    main()
