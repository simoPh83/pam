"""PlanIt API client.

Design seam: fetch_area() yields normalized record dicts. A second source
(planning.data.gov.uk, Camden Socrata, ...) can be added later by emitting
the same shape, without touching ledger/spreadsheet logic.

Verified API quirks (see docs/2026.09.27-planit-api-findings.md):
- start_date/end_date params filter the application's start_date (registration),
  NOT decided_date. Decided-date filtering must be done client-side.
- app_state param filters server-side and works.
- Small page sizes are flaky; use page_size >= 10.
- Service is slow (7-30s/page) and volunteer-run: one request at a time,
  delay between pages, generous timeouts, retry with backoff.

PlanIt's published etiquette (enforced below, after a 429 with
Retry-After ~5h on 2026-10-07 showed we were upsetting the service):
- run overnight only, 18:00-06:00 Europe/London (PlanIt's timezone;
  handles BST/GMT transitions — server location is irrelevant)
- minimum 60s between requests, adaptive backoff honouring Retry-After
- daily request cap of 300 (tracked per Europe/London day)
- User-Agent carries a contact email

Env overrides (Railway): PLANIT_DAILY_CAP, PLANIT_WINDOW_START,
PLANIT_WINDOW_END. Set PLANIT_WINDOW_START=0 and PLANIT_WINDOW_END=24 to
suspend the overnight window (e.g. the 2026-10-08 hunt catch-up).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Iterator
from zoneinfo import ZoneInfo

import requests

from .config import Area, Config

BASE_URL = "https://www.planit.org.uk/api/applics/json"
USER_AGENT = "pam-lead-monitor/0.1 (contact: simone.morciano@gmail.com)"

# PlanIt etiquette (see module docstring)
MIN_REQUEST_GAP = 60.0        # seconds between request starts
DAILY_REQUEST_CAP = int(os.environ.get("PLANIT_DAILY_CAP", "300"))
WINDOW_START_HOUR = int(os.environ.get("PLANIT_WINDOW_START", "18"))
WINDOW_END_HOUR = int(os.environ.get("PLANIT_WINDOW_END", "6"))
MAX_DEFER_SECONDS = 1800      # longer Retry-After -> requeue job instead of sleeping
PLANIT_TZ = ZoneInfo("Europe/London")

log = logging.getLogger(__name__)


class PlanItPaused(Exception):
    """Raised before sending a request that would break PlanIt's etiquette
    (outside the overnight window, daily cap reached, or a Retry-After too
    long to sleep through). Callers should resume work at `resume_at`."""

    def __init__(self, reason: str, resume_at: datetime) -> None:
        super().__init__(f"{reason}; resume at {resume_at:%Y-%m-%d %H:%M} {resume_at:%Z}")
        self.resume_at = resume_at


class UsageTracker:
    """Process-local per-day request counter. The worker installs a
    Postgres-backed one (set_usage_tracker) so the cap survives redeploys."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._count = 0

    def today(self) -> int:
        self._roll()
        return self._count

    def increment(self) -> None:
        self._roll()
        self._count += 1

    def _roll(self) -> None:
        today = datetime.now(PLANIT_TZ).date()
        if self._day != today:
            self._day, self._count = today, 0


_usage: UsageTracker = UsageTracker()
_last_request_started = 0.0
_gap_multiplier = 1  # doubled on each 429/403 (adaptive backoff)


def set_usage_tracker(tracker: UsageTracker) -> None:
    global _usage
    _usage = tracker


def in_window(now: datetime | None = None) -> bool:
    """True when PlanIt's overnight window (18:00-06:00 Europe/London) is open.
    WINDOW_START_HOUR=0 + WINDOW_END_HOUR=24 suspends the window entirely."""
    if WINDOW_START_HOUR == 0 and WINDOW_END_HOUR == 24:
        return True
    now = (now or datetime.now(PLANIT_TZ)).astimezone(PLANIT_TZ)
    return now.hour >= WINDOW_START_HOUR or now.hour < WINDOW_END_HOUR


def next_window_start(now: datetime | None = None) -> datetime:
    """Next opening of the window at/after `now` (timezone-aware)."""
    now = (now or datetime.now(PLANIT_TZ)).astimezone(PLANIT_TZ)
    if WINDOW_START_HOUR == 0 and WINDOW_END_HOUR == 24:
        return now  # window suspended: "next window" is right now
    start = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    return start if now < start else start + timedelta(days=1)


def _clamp_to_window(resume: datetime) -> datetime:
    return resume if in_window(resume) else next_window_start(resume)


def _gate() -> None:
    """Block until we may politely send one request; raise PlanItPaused when
    waiting is pointless (window closed or daily cap hit)."""
    global _last_request_started
    if not in_window():
        raise PlanItPaused("outside the 18:00-06:00 Europe/London window",
                           next_window_start())
    if _usage.today() >= DAILY_REQUEST_CAP:
        raise PlanItPaused(f"daily request cap ({DAILY_REQUEST_CAP}) reached",
                           next_window_start())
    wait = MIN_REQUEST_GAP * _gap_multiplier - (time.monotonic() - _last_request_started)
    if wait > 0:
        log.info("Pacing: %.0fs until next request", wait)
        time.sleep(wait)


def _get(params: dict, cfg: Config, url: str = BASE_URL) -> dict:
    global _last_request_started, _gap_multiplier
    headers = {"User-Agent": USER_AGENT}
    last_exc: Exception | None = None
    for attempt in range(cfg.max_retries):
        _gate()
        try:
            try:
                resp = requests.get(url, params=params, headers=headers,
                                    timeout=cfg.timeout_seconds)
            finally:
                # Count every attempt — the server saw it even if it failed
                _usage.increment()
                _last_request_started = time.monotonic()
            if resp.status_code in (429, 403):
                # PlanIt rate-limits aggressively (403 is a temporary block that can
                # outlast Retry-After); retrying early escalates the penalty
                _gap_multiplier = min(_gap_multiplier * 2, 8)
                retry_after = resp.headers.get("Retry-After")
                wait = (float(retry_after) + 60.0) if retry_after else 300.0 * (attempt + 1)
                if wait > MAX_DEFER_SECONDS:
                    # Hours-long penalty: requeue the job instead of blocking
                    # the worker (fetch progress is checkpointed, nothing lost)
                    raise PlanItPaused(
                        f"rate limited ({resp.status_code}) with a long Retry-After",
                        _clamp_to_window(datetime.now(PLANIT_TZ)
                                         + timedelta(seconds=wait)))
                resume = datetime.now() + timedelta(seconds=wait)
                log.warning("Rate limited (%d); waiting %.0fs (resume at %s) before retry %d/%d "
                            "(request gap now %.0fs)",
                            resp.status_code, wait, resume.strftime("%H:%M"),
                            attempt + 1, cfg.max_retries,
                            MIN_REQUEST_GAP * _gap_multiplier)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            wait = cfg.delay_seconds * (2 ** attempt)
            log.warning("Request failed (%s); retry %d/%d in %.0fs",
                        exc, attempt + 1, cfg.max_retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"PlanIt API unreachable after {cfg.max_retries} retries") from last_exc


def lookup_reference(authority: str, reference: str, cfg: Config) -> list[dict]:
    """Exact lookup of one application by reference within an authority
    (PlanIt `id_match`; spatial params are ignored when it is supplied)."""
    data = _get({"id_match": reference, "auth": authority, "pg_sz": 10}, cfg)
    return data.get("records") or []


def fetch_area(area: Area, states: list[str], start: date | None, end: date,
               cfg: Config, progress=None, since: date | None = None) -> Iterator[dict]:
    """Yield raw PlanIt records for one area, all states, paged.

    `progress` (optional) is a ledger-like object implementing
    progress(window, area, state) and save_progress(...) so an interrupted
    backfill can resume at the last completed page.

    The pseudo-state "All" omits the app_state filter, so every state
    (Rejected, Withdrawn, ...) comes back. `since` switches to incremental
    mode: records whose data changed on/after that date (PlanIt
    different_start), regardless of registration date.
    """
    window = f"since_{since}" if since else f"{start or 'all'}_{end}"
    for state in states:
        params: dict = {"pg_sz": cfg.page_size}
        if state != "All":
            params["app_state"] = state
        if since:
            params["different_start"] = since.isoformat()
        else:
            params["start_date"] = (start or end.replace(year=end.year - 20)).isoformat()
            params["end_date"] = end.isoformat()
        if area.postcode:
            params["pcode"] = area.postcode
            params["krad"] = area.radius_km or 5
        elif area.authority:
            params["auth"] = area.authority

        offset = 0
        if progress is not None:
            offset, done = progress.progress(window, area.name, state)
            if done:
                log.info("[%s/%s] already fetched for this window — skipping",
                         area.name, state)
                continue
            if offset:
                log.info("[%s/%s] resuming at offset %d", area.name, state, offset)

        while True:
            # PlanIt intermittently 400s on offset=0 — omit the param on page one
            if offset:
                params["offset"] = offset
            else:
                params.pop("offset", None)
            data = _get(params, cfg)
            records = data.get("records") or []
            log.info("[%s/%s] page at offset %d: %d records",
                     area.name, state, offset, len(records))
            yield from records
            finished = len(records) < cfg.page_size
            if progress is not None:
                progress.save_progress(window, area.name, state,
                                       offset + cfg.page_size, finished)
            if finished:
                break
            offset += cfg.page_size
            time.sleep(cfg.delay_seconds)
        time.sleep(cfg.delay_seconds)

