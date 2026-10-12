"""Sweep the unlinked_no_scheme queue: promote genuine standalone mains to
their own project and auto-resolve their review rows.

Per the scheme-grouping model a parent-less application that classifies as
'original' (description-keyword based — PlanIt's app_type is the form, not
the scheme role) is a legitimate root, not a review item. group() already
creates standalone projects for NEW originals; this script sweeps the
migration/orphans_to_review leftovers the worker never revisits.

For each pending unlinked_no_scheme row whose application classifies as
'original':
  - if the uid is already a project member (grouped since the row was
    created), refresh its stale stored role in place and re-derive the
    project's root/state — do NOT re-run group(), which could relocate the
    member into an unrelated address bucket;
  - otherwise run it through projects.group() (joins an address scheme if
    one has appeared since, else becomes a standalone clean project).
  Either way the review row is auto-resolved with resolution='obsolete'.

Idempotent. Dry-run by default (rolls back); --apply commits.
See docs/2026-10-12-no-scheme-queue-notes.md Issue 1.

  python scripts/promote_standalone_originals.py          # dry-run
  python scripts/promote_standalone_originals.py --apply
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
from pam.projects import _set_root_and_state, group, role_of

log = logging.getLogger("promote_standalone_originals")


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
        rows = conn.execute(
            "SELECT g.id, g.uid, g.authority, a.description, a.app_type "
            "FROM grouping_review g "
            "JOIN applications a ON a.uid = g.uid "
            "WHERE g.status = 'pending' AND g.reason = 'unlinked_no_scheme'"
        ).fetchall()
        stats["queue_rows"] = len(rows)

        originals = [(gid, uid, auth, desc, app_type)
                     for gid, uid, auth, desc, app_type in rows
                     if role_of(desc, app_type, auth) == "original"]
        stats["classified_original"] = len(originals)

        member_pids: dict[str, set[int]] = {}
        for uid, pid in conn.execute(
                "SELECT uid, project_id FROM project_applications "
                "WHERE uid = ANY(%s)",
                ([u for _g, u, *_ in originals] or [None],)).fetchall():
            member_pids.setdefault(uid, set()).add(pid)
        stats["already_grouped"] = len(member_pids)

        # Not grouped yet: run through group() — joins an address scheme if
        # one has appeared since, else becomes a standalone clean project.
        to_group = [uid for _g, uid, *_ in originals if uid not in member_pids]
        stats["to_promote"] = len(to_group)
        for i in range(0, len(to_group), 500):
            group(to_group[i:i + 500], conn)
            log.info("Grouped %s/%s", min(i + 500, len(to_group)),
                     len(to_group))

        # Already grouped: refresh the stale stored role in place (these rows
        # were classified by an older role_of — e.g. "air condition unit"
        # read as a planning condition) and re-derive their projects' state.
        # Deliberately NOT group(): that could relocate a member into the
        # address bucket of an unrelated scheme.
        stale_pids: set[int] = set()
        for _gid, uid, _auth, _desc, _at in originals:
            if uid not in member_pids:
                continue
            cur = conn.execute(
                "UPDATE project_applications SET role = 'original' "
                "WHERE uid = %s AND role IS DISTINCT FROM 'original' "
                "AND role_override IS NULL", (uid,))  # never stomp manual roles
            if cur.rowcount:
                stats["roles_refreshed"] += cur.rowcount
                stale_pids |= member_pids[uid]
        for pid in stale_pids:
            _set_root_and_state(conn, pid)
        stats["projects_rederived"] = len(stale_pids)

        cur = conn.execute(
            "UPDATE grouping_review SET status = 'resolved', "
            "resolution = 'obsolete', resolved_at = now() "
            "WHERE status = 'pending' AND reason = 'unlinked_no_scheme' "
            "AND id = ANY(%s)", ([gid for gid, *_ in originals],))
        stats["reviews_resolved"] = cur.rowcount

        stats["queue_after"] = conn.execute(
            "SELECT count(*) FROM grouping_review WHERE status = 'pending' "
            "AND reason = 'unlinked_no_scheme'").fetchone()[0]
        promoted_uids = [u for _g, u, *_ in originals]
        stats["clean_standalone_projects"] = conn.execute(
            "SELECT count(*) FROM projects p WHERE p.grouping_state = 'clean' "
            "AND p.n_applications = 1 AND p.root_uid = ANY(%s)",
            (promoted_uids or [None],)).fetchone()[0]
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
