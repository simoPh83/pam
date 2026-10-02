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

from .config import DEFAULT_CONFIG_PATH, load_config, load_dotenv
from .ledger import Ledger
from .main import execute

log = logging.getLogger("pam.worker")

MAX_ATTEMPTS = 3
RETRY_DELAY = timedelta(minutes=10)
POLL_SECONDS = 30
SYNC_HOUR_UTC = int(os.environ.get("SYNC_HOUR_UTC", "5"))
SYNC_DAYS = int(os.environ.get("SYNC_DAYS", "2"))  # --since = today - SYNC_DAYS

JOBS_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,              -- backfill | sync
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
            priority: int = 0) -> bool:
    key = f"{kind}:{area}:{start}:{end}:{since}"
    cur = conn.execute(
        "INSERT INTO jobs (kind, area, start_date, end_date, since, priority, dedupe_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING",
        (kind, area, start, end, since, priority, key),
    )
    return cur.rowcount == 1


def area_names() -> list[str]:
    return [a.name for a in load_config(argv=[]).areas]


def enqueue_sync(conn, days: int = SYNC_DAYS) -> int:
    since = date.today() - timedelta(days=days)
    return sum(enqueue(conn, kind="sync", area=name, since=since, priority=100)
               for name in area_names())


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
    argv = ["--area", job["area"]]
    if job["kind"] == "sync":
        argv += ["--since", job["since"].isoformat()]
    else:
        argv += ["--from", job["start_date"].isoformat(),
                 "--to", job["end_date"].isoformat()]
    return argv


def process(conn, job: dict) -> None:
    log.info("Job %s start: %s %s (attempt %s)", job["id"], job["kind"],
             job["area"], job["attempts"])
    try:
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


if __name__ == "__main__":
    main()
