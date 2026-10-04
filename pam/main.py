"""Orchestrates a monitoring run: fetch -> dedupe/track -> update leads sheet."""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

import requests

from .api import fetch_area
from .config import load_config
from .ledger import Ledger
from .spreadsheet import LeadsSheet
from .transform import matches_filters, normalize

log = logging.getLogger("pam")

RECENT_WINDOW_DAYS = 14  # rolling window for daily runs (post-backfill)


def geocode_home(postcode: str) -> tuple[float, float] | None:
    try:
        resp = requests.get(f"https://api.postcodes.io/postcodes/{postcode}",
                            timeout=20)
        resp.raise_for_status()
        result = resp.json()["result"]
        return float(result["latitude"]), float(result["longitude"])
    except Exception as exc:
        log.warning("Could not geocode home postcode %s: %s", postcode, exc)
        return None


def run(argv: list[str] | None = None) -> int:
    return execute(argv)[0]


def execute(argv: list[str] | None = None) -> tuple[int, dict]:
    """Run one fetch; returns (exit code, stats)."""
    cfg = load_config(argv=argv)
    args = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in args

    if "--log" in args:
        from pathlib import Path
        log_path = Path(args[args.index("--log") + 1])
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)

    home = geocode_home(cfg.home_postcode)
    today = date.today()
    since = cfg.cli_since
    start = cfg.cli_start_date or cfg.backfill_start or (today - timedelta(days=RECENT_WINDOW_DAYS))
    if cfg.cli_start_date and not cfg.cli_end_date and not since:
        # The resume key embeds the end date; a floating "today" would start a
        # fresh window (and re-fetch everything) on every new day.
        log.error("--from requires --to so interrupted runs can resume")
        return 2, {}
    end = cfg.cli_end_date or today
    # Backfill rule: don't list leads decided before the window. In incremental
    # mode there is no window, so allow decisions up to 30 days older than the
    # change date (PlanIt scrapes a decision some days after it is made).
    lead_cutoff = since - timedelta(days=30) if since else start
    if since:
        log.info("Incremental sync: changed since %s | areas: %s", since,
                 ", ".join(a.name for a in cfg.areas))
    else:
        log.info("Window: %s -> %s | areas: %s", start, end, ", ".join(a.name for a in cfg.areas))

    ledger = Ledger(cfg.database_url)
    # With Postgres, leads are a query on applications.in_leads_sheet; the xlsx
    # is only maintained for local SQLite runs.
    sheet = None if cfg.database_url else LeadsSheet(cfg.leads_path)
    wanted_states = (["All"] if cfg.fetch_all_states
                     else list(dict.fromkeys(cfg.lead_states + cfg.watch_states)))

    stats = {"fetched": 0, "new_tracked": 0, "leads_added": 0, "leads_updated": 0}

    try:
        for area in cfg.areas:
            # A dry run must not record fetch progress, or the real run would skip it
            for raw in fetch_area(area, wanted_states, start, end, cfg,
                                  progress=None if dry_run else ledger, since=since):
                row = normalize(raw, area.name, home)
                if not matches_filters(row, cfg):
                    continue
                stats["fetched"] += 1

                prev_state = ledger.get_state(row["uid"])
                is_lead = row["app_state"] in cfg.lead_states
                already_listed = ledger.in_leads_sheet(row["uid"])

                if is_lead and not already_listed and row["decided_date"] and row["decided_date"] < lead_cutoff:
                    ledger.upsert(row, in_leads=False, raw=raw)
                    stats["new_tracked"] += 1
                    continue

                state_changed = (
                    is_lead and already_listed is False and prev_state in cfg.watch_states
                ) or (
                    is_lead and already_listed and prev_state != row["app_state"]
                )

                if is_lead:
                    if not already_listed or state_changed:
                        log.info("LEAD %s [%s] %s — %s",
                                 "CHANGED:" if state_changed else "new:",
                                 row["app_state"], row["uid"], (row["address"] or "")[:60])
                        if not dry_run:
                            action = (sheet.upsert(row, state_changed=state_changed)
                                      if sheet else
                                      ("updated" if already_listed else "added"))
                            stats[f"leads_{action}"] += 1
                        else:
                            stats["leads_added" if not already_listed else "leads_updated"] += 1
                    # else: lead already listed, unchanged — skip silently
                elif prev_state is None:
                    stats["new_tracked"] += 1

                if not dry_run:
                    ledger.upsert(row, in_leads=is_lead or already_listed, raw=raw)

        if not dry_run and sheet:
            sheet.save()
    finally:
        ledger.close()

    log.info("Done. fetched=%(fetched)d newly_tracked=%(new_tracked)d "
             "leads_added=%(leads_added)d leads_updated=%(leads_updated)d", stats)
    print(f"\nRun summary: {stats['fetched']} fetched, {stats['new_tracked']} newly tracked, "
          f"{stats['leads_added']} leads added, {stats['leads_updated']} updated"
          + ("  [DRY RUN — nothing written]" if dry_run else ""))
    return 0, stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run())


if __name__ == "__main__":
    main()
