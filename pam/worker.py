"""Job-queue worker: runs backfills and daily incremental syncs against Postgres.

    python -m pam.worker run                      # loop forever (Railway entrypoint)
    python -m pam.worker once                     # process pending jobs, then exit
    python -m pam.worker enqueue-backfill --from 2023-10-01 --to 2026-10-01 \
        [--area camden ...] [--priority 10]
    python -m pam.worker enqueue-sync [--days 2]  # one --since job per area
    python -m pam.worker status

Jobs live in the `jobs` table. Fetch progress is stored per window in
`fetch_progress`, so a job interrupted by a redeploy simply resumes.
Run ONE worker only: PlanIt rate-limits per IP.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import psycopg

from . import projects
from .api import lookup_reference
from .config import DEFAULT_CONFIG_PATH, load_config, load_dotenv
from .ledger import Ledger
from .main import execute, geocode_home
from .transform import normalize

log = logging.getLogger("pam.worker")

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=10)
POLL_SECONDS = 30
SYNC_HOUR_UTC = int(os.environ.get("SYNC_HOUR_UTC", "5"))
SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "2"))  # --since = today - SYNC_DAYS
# Off by default: parent hunts are only enqueued manually (enqueue-parent-hunt).
AUTO_PARENT_HUNT = os.environ.get("AUTO_PARENT_HUNT", "0") == "1"
# Pause between single-reference PlanIt lookups (seconds)
HUNT_DELAY = float(os.environ.get("HUNT_DELAY_SECONDS", "10"))

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,              -- backfill | sync | parent_hunt
    area        TEXT NOT NULL,
    start_date  DATE,
    end_date    DATE,
    since       DATE,
    priority    INTEGER NOT NULL DEFAULT 0, -- higher runs first
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    not_before  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    stats       JSONB,
    error       TEXT,
    dedupe_key  TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs (status, priority DESC, id)
"""


def connect() -> psycopg.Connection:
    load_dotenv(DEFAULT_CONFIG_PATH.parent / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    conn = psycopg.connect(url, autocommit=True)
    for stmt in JOBS_DDL.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    return conn


def enqueue(conn, *, kind: str, area: str, start: date | None = None,
            end: date | None = None, since: date | None = None,
            priority: int = 0, dedupe_suffix: str = "") -> bool:
    key = f"{kind}:{area}:{start}:{end}:{since}{dedupe_suffix}"
    cur = conn.execute(
        "INSERT INTO jobs (kind, area, start_date, end_date, since, priority, dedupe_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING",
        (kind, area, start, end, since, priority, key),
    )
    return cur.rowcount == 1


def area_names() -> list[str]:
    return [a.name for a in load_config(argv=[]).areas]


def sync_since(conn, area: str) -> date:
    """Per-area watermark: start of the last successful sync (minus 1 day overlap).

    Uses started_at (not finished_at) so changes made during a run are re-seen.
    A new area with no sync yet starts from its first backfill, so changes made
    while the backfill was running aren't missed; otherwise today - SYNC_DAYS.
    """
    row = conn.execute(
        """
        SELECT COALESCE(
            (SELECT max(started_at) FROM jobs
             WHERE kind = 'sync' AND status = 'done' AND area = %(a)s),
            (SELECT min(started_at) FROM jobs
             WHERE kind = 'backfill' AND area = %(a)s AND started_at IS NOT NULL))
        """, {"a": area}).fetchone()
    if row and row[0]:
        return row[0].date() - timedelta(days=1)
    return date.today() - timedelta(days=SYNC_DAYS)


def enqueue_sync(conn, days: int | None = None) -> int:
    """days=None: automatic per-area watermark; days=N: manual override."""
    added = 0
    for name in area_names():
        if days is not None:
            since = date.today() - timedelta(days=days)
        else:
            busy = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = 'sync' AND area = %s "
                "AND (status IN ('pending', 'running') "
                "     OR created_at >= date_trunc('day', now()))", (name,)).fetchone()
            if busy:
                continue  # at most one sync per area per UTC day
            since = sync_since(conn, name)
        added += enqueue(conn, kind="sync", area=name, since=since, priority=100)
    return added


def claim(conn) -> dict | None:
    row = conn.execute(
        """
        UPDATE jobs SET status = 'running', started_at = now(), attempts = attempts + 1
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'pending' AND not_before <= now()
            ORDER BY priority DESC, id
            FOR UPDATE SKIP LOCKED LIMIT 1)
        RETURNING id, kind, area, start_date, end_date, since, attempts
        """
    ).fetchone()
    if not row:
        return None
    keys = ("id", "kind", "area", "start_date", "end_date", "since", "attempts")
    return dict(zip(keys, row))


def job_argv(job: dict) -> list[str]:
    area = job["area"]
    if job["kind"] == "parent_hunt":
        configured_area = next(
            (a.name for a in load_config(argv=[]).areas if a.authority == area),
            None,
        )
        if configured_area is None:
            raise ValueError(f"No configured area for authority '{area}'")
        area = configured_area

    argv = ["--area", area]
    if job["kind"] == "sync":
        argv += ["--since", job["since"].isoformat()]
    elif job["kind"] != "parent_hunt":
        argv += ["--from", job["start_date"].isoformat(),
                 "--to", job["end_date"].isoformat()]
    return argv


def process(conn, job: dict) -> None:
    log.info("Job %s start: %s %s (attempt %s)", job["id"], job["kind"],
             job["area"], job["attempts"])
    try:
        if job["kind"] == "parent_hunt":
            stats = run_parent_hunt(conn, job)
        else:
            code, stats = execute(job_argv(job))
            if code != 0:
                raise RuntimeError(f"pam exited with code {code}")
    except (Exception, SystemExit) as exc:
        err = traceback.format_exc() if isinstance(exc, Exception) else str(exc)
        log.error("Job %s failed: %s", job["id"], exc)
        final = job["attempts"] >= MAX_ATTEMPTS
        conn.execute(
            "UPDATE jobs SET status = %s, error = %s, finished_at = now(), "
            "not_before = %s WHERE id = %s",
            ("failed" if final else "pending", err[-4000:],
             datetime.now(timezone.utc) + RETRY_DELAY, job["id"]),
        )
        return
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = now(), stats = %s::jsonb, "
        "error = NULL WHERE id = %s",
        (json.dumps(stats), job["id"]),
    )
    log.info("Job %s done: %s", job["id"], stats)


def run_parent_hunt(conn, job: dict) -> dict:
    """Look up each pending missing parent of one authority by exact reference
    (PlanIt id_match). Only the matching application is stored (never as a
    lead); the parent and the children citing it are then regrouped. A lookup
    is definitive, so a miss marks the ref 'exhausted'. Progress is per ref, so
    an interrupted job simply resumes. Parents may cite older parents; those
    new refs are picked up by the same job."""
    cfg = load_config(argv=job_argv(job))
    area = cfg.areas[0]
    home = geocode_home(cfg.home_postcode)
    ledger = Ledger(cfg.database_url)
    stats = {"looked_up": 0, "found": 0, "not_found": 0}
    try:
        while True:
            pending = conn.execute(
                "SELECT id, reference, requested_by FROM missing_parents "
                "WHERE authority = %s AND status = 'pending' AND ref_year IS NOT NULL "
                "ORDER BY ref_year DESC, id LIMIT 200", (area.authority,)).fetchall()
            if not pending:
                break
            for mp_id, ref, requested_by in pending:
                match = None
                for raw in lookup_reference(area.authority, ref, cfg):
                    row = normalize(raw, area.name, home)
                    if str(row["reference"] or "").strip().upper() == ref:
                        match = (row, raw)
                        break
                stats["looked_up"] += 1
                if match:
                    row, raw = match
                    ledger.upsert(row, in_leads=False, raw=raw)
                    conn.execute(
                        "UPDATE missing_parents SET status = 'found', found_uid = %s, "
                        "attempts = attempts + 1 WHERE id = %s", (row["uid"], mp_id))
                    children = {r[0] for r in conn.execute(
                        "SELECT pa.uid FROM project_applications pa "
                        "JOIN projects p ON p.id = pa.project_id "
                        "WHERE p.authority = %s AND pa.parent_ref = %s",
                        (area.authority, ref))}
                    if requested_by:
                        children.add(requested_by)
                    projects.group({row["uid"]} | children, ledger.conn)
                    ledger.conn.commit()
                    stats["found"] += 1
                else:
                    conn.execute(
                        "UPDATE missing_parents SET status = 'exhausted', "
                        "attempts = attempts + 1 WHERE id = %s", (mp_id,))
                    stats["not_found"] += 1
                if stats["looked_up"] % 50 == 0:
                    log.info("Parent hunt %s: %s", area.authority, stats)
                    conn.execute("UPDATE jobs SET stats = %s::jsonb WHERE id = %s",
                                 (json.dumps(stats), job["id"]))
                time.sleep(HUNT_DELAY)
    finally:
        ledger.close()
    return stats


def requeue_interrupted(conn) -> None:
    """A redeploy kills the worker mid-job; those jobs resume from fetch_progress."""
    n = conn.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'").rowcount
    if n:
        log.info("Re-queued %d interrupted job(s)", n)


def maybe_schedule_sync(conn) -> None:
    if datetime.now(timezone.utc).hour >= SYNC_HOUR_UTC:
        added = enqueue_sync(conn)  # deduped per day by the since date
        if added:
            log.info("Scheduled %d daily sync job(s)", added)
        if AUTO_PARENT_HUNT:
            hunted = enqueue_parent_hunt(conn)  # deduped; no-op when none pending
            if hunted:
                log.info("Scheduled %d parent-hunt job(s)", hunted)


def loop(once: bool) -> None:
    conn = connect()
    Ledger(os.environ["DATABASE_URL"]).close()  # make sure the data schema exists
    requeue_interrupted(conn)
    while True:
        if not once:
            maybe_schedule_sync(conn)
        job = claim(conn)
        if job:
            process(conn, job)
            continue
        if once:
            return
        time.sleep(POLL_SECONDS)


def status(conn) -> None:
    for row in conn.execute(
        "SELECT status, kind, count(*) FROM jobs GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        print(*row)
    print("-- recent --")
    for row in conn.execute(
        "SELECT id, kind, area, status, attempts, left(coalesce(error, ''), 60) "
        "FROM jobs ORDER BY id DESC LIMIT 15"
    ):
        print(*row)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="pam.worker")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("status")
    bf = sub.add_parser("enqueue-backfill")
    bf.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    bf.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    bf.add_argument("--area", action="append",
                    help="repeatable; default = every area in config.yaml")
    bf.add_argument("--priority", type=int, default=0)
    sy = sub.add_parser("enqueue-sync")
    sy.add_argument("--days", type=int, default=SYNC_DAYS)
    ph = sub.add_parser("enqueue-parent-hunt",
                        help="one job per authority with pending missing parents")
    args = parser.parse_args()

    if args.cmd in ("run", "once"):
        loop(once=args.cmd == "once")
        return
    conn = connect()
    if args.cmd == "status":
        status(conn)
    elif args.cmd == "enqueue-backfill":
        names = args.area or area_names()
        added = sum(enqueue(conn, kind="backfill", area=n, start=args.start,
                            end=args.end, priority=args.priority) for n in names)
        print(f"Enqueued {added} of {len(names)} backfill job(s)")
    elif args.cmd == "enqueue-sync":
        print(f"Enqueued {enqueue_sync(conn, args.days)} sync job(s)")
    elif args.cmd == "enqueue-parent-hunt":
        print(f"Enqueued {enqueue_parent_hunt(conn)} parent-hunt job(s)")


def enqueue_parent_hunt(conn) -> int:
    """One job per authority that still has pending missing parents. Skipped
    while that authority already has a pending/running hunt; the dedupe key
    carries the date so a new job can be queued on a later day."""
    rows = conn.execute(
        """
        SELECT mp.authority FROM missing_parents mp
        WHERE mp.status = 'pending' AND mp.ref_year IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind = 'parent_hunt'
                          AND j.area = mp.authority
                          AND j.status IN ('pending', 'running'))
        GROUP BY 1 ORDER BY count(*) DESC
        """).fetchall()
    return sum(enqueue(conn, kind="parent_hunt", area=a, priority=50,
                       dedupe_suffix=f":{date.today()}") for (a,) in rows)


if __name__ == "__main__":
    main()
