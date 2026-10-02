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
"""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Iterator

import requests

from .config import Area, Config

BASE_URL = "https://www.planit.org.uk/api/applics/json"
USER_AGENT = "pam-lead-monitor/0.1 (personal research; github.com/planit)"

log = logging.getLogger(__name__)


def _get(params: dict, cfg: Config, url: str = BASE_URL) -> dict:
    headers = {"User-Agent": USER_AGENT}
    last_exc: Exception | None = None
    for attempt in range(cfg.max_retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=cfg.timeout_seconds)
            if resp.status_code == 429:
                # PlanIt rate-limits aggressively; respect Retry-After, else long backoff
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else 30.0 * (attempt + 1)
                log.warning("Rate limited (429); waiting %.0fs before retry %d/%d",
                            wait, attempt + 1, cfg.max_retries)
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

