"""Configuration loading: YAML defaults, overridable via CLI flags."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class Area:
    name: str
    postcode: str | None = None
    radius_km: float | None = None
    authority: str | None = None


@dataclass
class Config:
    home_postcode: str
    areas: list[Area]
    lead_states: list[str]
    watch_states: list[str]
    app_size: str | None
    keywords: list[str] | None
    backfill_start: date | None
    page_size: int
    delay_seconds: float
    timeout_seconds: int
    max_retries: int
    leads_path: Path
    ledger_path: Path
    database_url: str | None = None  # postgres:// URL overrides ledger_path
    exclude_app_types: list[str] = field(default_factory=list)
    # CLI-only modifiers
    cli_start_date: date | None = None
    cli_end_date: date | None = None
    cli_since: date | None = None
    fetch_all_states: bool = True


def load_dotenv(path: Path) -> None:
    """Load KEY=value lines from a .env file; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value))


def load_config(path: Path = DEFAULT_CONFIG_PATH, argv: list[str] | None = None) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    parser = argparse.ArgumentParser(
        prog="pam",
        description="Monitor planning applications via the PlanIt API.",
    )
    parser.add_argument("--config", type=Path, default=path, help="Path to config YAML")
    parser.add_argument("--from", dest="start_date", metavar="YYYY-MM-DD",
                        help="Override query window start (PlanIt start_date)")
    parser.add_argument("--to", dest="end_date", metavar="YYYY-MM-DD",
                        help="Override query window end (default: today)")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Incremental sync: records changed on/after this date "
                             "(PlanIt last_different), any state, any start_date")
    parser.add_argument("--states", nargs="+", metavar="STATE",
                        help="Extra app_states to fetch, e.g. --states Withdrawn Rejected")
    parser.add_argument("--no-backfill", action="store_true",
                        help="Skip historical backfill; query only the last 14 days")
    parser.add_argument("--area", metavar="NAME",
                        help="Run only the named area from config")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and report, but write nothing to ledger or spreadsheet")
    parser.add_argument("--log", type=Path, metavar="FILE",
                        help="Also write the progress log to FILE (append)")
    args = parser.parse_args(argv)

    if args.config != path:
        raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))

    defaults = raw.get("defaults", {})
    backfill = raw.get("backfill", {})
    api = raw.get("api", {})
    output = raw.get("output", {})

    areas = [Area(**a) for a in raw.get("areas", [])]
    if args.area:
        areas = [a for a in areas if a.name == args.area]
        if not areas:
            sys.exit(f"No area named '{args.area}' in config")
    if not areas:
        sys.exit("Config defines no areas to monitor")

    lead_states = list(defaults.get("lead_states", ["Permitted", "Conditions"]))
    watch_states = list(defaults.get("watch_states", ["Undecided"]))
    if args.states:
        watch_states = list(dict.fromkeys(watch_states + args.states))

    backfill_start = _parse_date(backfill.get("start_date")) if backfill.get("enabled") else None
    if args.no_backfill:
        backfill_start = None

    root = args.config.resolve().parent
    return Config(
        home_postcode=raw["home_postcode"],
        areas=areas,
        lead_states=lead_states,
        watch_states=watch_states,
        app_size=defaults.get("app_size"),
        keywords=defaults.get("keywords"),
        exclude_app_types=list(defaults.get("exclude_app_types", [])),
        backfill_start=backfill_start,
        page_size=int(api.get("page_size", 100)),
        delay_seconds=float(api.get("delay_seconds", 2)),
        timeout_seconds=int(api.get("timeout_seconds", 120)),
        max_retries=int(api.get("max_retries", 4)),
        leads_path=root / output.get("leads_path", "data/leads.xlsx"),
        ledger_path=root / output.get("ledger_path", "data/ledger.sqlite"),
        database_url=os.environ.get("DATABASE_URL") or None,
        cli_start_date=_parse_date(args.start_date),
        cli_end_date=_parse_date(args.end_date),
        cli_since=_parse_date(args.since),
        fetch_all_states=bool(defaults.get("fetch_all_states", True)),
    )
