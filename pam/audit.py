"""Borough audit: probe each council's PlanIt feed and record what data it
actually carries (agent names, applicant names, docs links, tree-works share,
crawl freshness).

Continues docs/2026.09.27-borough-audit.csv — fills rows where fetched = -1.

Method (matches the rows already audited): one 100-record page per authority,
filtered to applications *started* in the last N days (default 90, same window
as the backfill). A stale council crawl shows up as few records and an old
latest_scrape (cf. Barking & Dagenham, 2026-07-06).

Usage:
    python -m pam.audit                          # audit pending rows (fetched = -1)
    python -m pam.audit --refresh                # re-audit every borough
    python -m pam.audit --authority Enfield "Tower Hamlets"
    python -m pam.audit --days 30 --dry-run      # narrower window, print only
    python -m pam.audit --areas                  # download/cache the full area list
    python -m pam.audit --areas --match kensing  #   ...and grep it for a name
    python -m pam.audit --log docs/audit.log     # tee progress log into a file
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import requests
import yaml

from .api import _get  # shared retry/backoff/429 handling — keep one copy
from .config import DEFAULT_CONFIG_PATH
from .transform import _agent_name, _applicant_name

log = logging.getLogger("pam.audit")

CSV_PATH = Path(__file__).resolve().parent.parent / "docs" / "2026.09.27-borough-audit.csv"
AREAS_CACHE = Path(__file__).resolve().parent.parent / "docs" / "planit-areas.json"

FIELDNAMES = ["authority", "fetched", "agent_names", "applicant_names",
              "with_docs_url", "trees_pct", "latest_scrape", "sample_agent"]

SAMPLE_SIZE = 100       # one page is enough for a data-quality snapshot
DEFAULT_WINDOW_DAYS = 90

# Display name -> PlanIt `auth` param, for councils whose feed name differs.
# The CSV already annotates known cases, e.g. "City of London (as 'City')" —
# those annotations are parsed first; this dict is for new discoveries.
# Resolved 2026-09-28 from docs/planit-areas.json:
ALIASES: dict[str, str] = {
    "Kensington and Chelsea": "Kensington",   # max_date 2026-07-27 — stale crawl
    "Kingston upon Thames": "Kingston",
    "Richmond upon Thames": "Richmond",
}

_AS_PATTERN = re.compile(r"\(as '([^']+)'\)")


def auth_param_for(authority_cell: str) -> str:
    """Resolve the PlanIt `auth` value for a CSV authority cell."""
    annotated = _AS_PATTERN.search(authority_cell)
    if annotated:
        return annotated.group(1)
    return ALIASES.get(authority_cell, authority_cell)


def load_pacing(config_path: Path) -> SimpleNamespace:
    """Read the `api:` pacing section of config.yaml into the attributes
    pam.api._get expects (keeps audit throttling identical to real runs)."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    api = raw.get("api", {})
    return SimpleNamespace(
        page_size=SAMPLE_SIZE,
        delay_seconds=float(api.get("delay_seconds", 5)),
        timeout_seconds=int(api.get("timeout_seconds", 120)),
        max_retries=int(api.get("max_retries", 6)),
    )


def audit_authority(authority_cell: str, days: int, pacing) -> dict:
    """Fetch one sample page for a council and compute the audit stats."""
    auth = auth_param_for(authority_cell)
    today = date.today()
    params = {
        "auth": auth,
        "start_date": (today - timedelta(days=days)).isoformat(),
        "end_date": today.isoformat(),
        "pg_sz": SAMPLE_SIZE,
    }
    data = _get(params, pacing)
    records = data.get("records") or []

    agents = [a for a in (_agent_name(r) for r in records) if a]
    scrapes = [str(r["last_scraped"])[:10] for r in records if r.get("last_scraped")]
    trees = sum(1 for r in records if r.get("app_type") == "Trees")
    fetched = len(records)

    return {
        "fetched": fetched,
        "agent_names": len(agents),
        "applicant_names": sum(1 for r in records if _applicant_name(r)),
        "with_docs_url": sum(1 for r in records
                             if (r.get("other_fields") or {}).get("docs_url")),
        "trees_pct": round(100 * trees / fetched) if fetched else "",
        "latest_scrape": max(scrapes) if scrapes else "",
        "sample_agent": agents[0] if agents else "",
    }


def is_pending(row: dict) -> bool:
    return row.get("fetched", "").strip() in ("", "-1")


def fetch_areas(cache: Path, pacing) -> list[dict]:
    """Download all PlanIt areas (paged, ~10/page, ~50 pages of tiny fast
    responses) and cache them. Each record carries area_name, long_name,
    scraper_name, total, max_date, is_planning, borders geometry, etc."""
    areas, offset = [], 0
    while True:
        params = {"offset": offset} if offset else {}
        data = _get(params, pacing, url="https://www.planit.org.uk/api/areas/json")
        records = data.get("records") or []
        areas.extend(records)
        total = data.get("total", 0)
        log.info("areas: %d/%d", len(areas), total)
        # checkpoint after every page — a 429 ban or Ctrl-C loses nothing
        cache.write_text(json.dumps(areas, indent=1), encoding="utf-8")
        if len(areas) >= total or not records:
            break
        offset += len(records)
    log.info("cached %d areas -> %s", len(areas), cache)
    return areas


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pam-audit",
        description="Audit council PlanIt feeds into the borough-audit CSV.",
    )
    parser.add_argument("--csv", type=Path, default=CSV_PATH,
                        help="Audit CSV to read/update")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH,
                        help="Config YAML (only the api: pacing section is used)")
    parser.add_argument("--refresh", action="store_true",
                        help="Re-audit all rows, not just pending ones")
    parser.add_argument("--authority", nargs="+", metavar="NAME",
                        help="Audit only these CSV authority names")
    parser.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS,
                        help="start_date window in days (default: 90)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results without writing the CSV")
    parser.add_argument("--areas", action="store_true",
                        help="Download/cache the full PlanIt area list instead "
                             "of auditing (uses cache if present and fresh today)")
    parser.add_argument("--match", metavar="TEXT",
                        help="With --areas: print areas whose names contain TEXT")
    parser.add_argument("--log", type=Path, metavar="FILE",
                        help="Also write the progress log to FILE (append)")
    args = parser.parse_args(argv)

    if args.log:
        handler = logging.FileHandler(args.log, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)

    pacing = load_pacing(args.config)

    if args.areas:
        cached: list | None = None
        if AREAS_CACHE.exists() and date.fromtimestamp(
                AREAS_CACHE.stat().st_mtime) == date.today():
            data = json.loads(AREAS_CACHE.read_text(encoding="utf-8"))
            if isinstance(data, list):  # a dict would be a one-page envelope
                cached = data
        if cached is not None:
            areas = cached
            log.info("using today's cache: %s (%d areas)", AREAS_CACHE, len(areas))
        else:
            areas = fetch_areas(AREAS_CACHE, pacing)
        planning = [a for a in areas if a.get("is_planning")]
        print(f"{len(areas)} areas cached ({len(planning)} planning authorities) "
              f"-> {AREAS_CACHE}")
        if args.match:
            needle = args.match.lower()
            hits = [a for a in areas
                    if needle in f"{a.get('area_name')}|{a.get('long_name')}"
                                   f"|{a.get('scraper_name')}".lower()]
            for a in hits:
                print(f"  {a.get('area_name')!r:40} long={a.get('long_name')!r} "
                      f"scraper={a.get('scraper_name')!r} total={a.get('total')} "
                      f"max_date={a.get('max_date')} planning={a.get('is_planning')}")
            if not hits:
                print(f"  no area names match {args.match!r}")
        return 0

    with args.csv.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    if args.authority:
        wanted = set(args.authority)
        targets = [r for r in rows if r["authority"] in wanted]
        missing = wanted - {r["authority"] for r in targets}
        if missing:
            sys.exit(f"Not in CSV: {', '.join(sorted(missing))}")
    elif args.refresh:
        targets = rows
    else:
        targets = [r for r in rows if is_pending(r)]

    if not targets:
        print("Nothing to audit — no pending rows. Use --refresh to redo all.")
        return 0

    log.info("Auditing %d authorities (window: last %d days)",
             len(targets), args.days)

    failures = 0
    for i, row in enumerate(targets):
        auth = auth_param_for(row["authority"])
        log.info("[%d/%d] %s (auth=%r) ...", i + 1, len(targets),
                 row["authority"], auth)
        try:
            stats = audit_authority(row["authority"], args.days, pacing)
        except RuntimeError as exc:
            failures += 1
            log.error("%s: fetch failed, leaving as pending (%s)",
                      row["authority"], exc)
            continue
        row.update({k: str(v) for k, v in stats.items()})
        log.info("  -> fetched=%(fetched)s agents=%(agent_names)s "
                 "applicants=%(applicant_names)s docs=%(with_docs_url)s "
                 "trees%%=%(trees_pct)s scraped=%(latest_scrape)s", stats)
        if stats["fetched"] == "0":
            log.warning("  0 records — the auth name may differ; "
                        "check planit.org.uk and add it to ALIASES")
        if i < len(targets) - 1:
            time.sleep(pacing.delay_seconds)

    if args.dry_run:
        print("\n[DRY RUN] results:")
        for r in targets:
            print(" ", ",".join(r.get(f, "") for f in FIELDNAMES))
    else:
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nCSV updated: {args.csv}")

    pending_left = sum(1 for r in rows if is_pending(r))
    print(f"Audited {len(targets) - failures}/{len(targets)}; "
          f"{pending_left} still pending.")
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
