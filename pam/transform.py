"""Normalize raw PlanIt records into the shape the ledger and spreadsheet use.

Unique key: PlanIt's `name` (e.g. "Islington/P031722"), falling back to
area_id + uid. Top-level `reference` is often null — never use it as a key.
"""

from __future__ import annotations

import math
from datetime import date

# other_fields keys seen holding agent info across councils (mostly junk —
# "See source" — but cheap to check; real values appear in some councils)
_AGENT_KEYS = ("agent_name", "agent", "agent_company", "agent_organisation")
_JUNK_VALUES = {"see source", "n/a", "na", "not supplied", "unknown", "-"}


def uid_of(raw: dict) -> str:
    name = raw.get("name")
    if name:
        return str(name)
    return f"{raw.get('area_id')}/{raw.get('uid')}"


def _agent_name(raw: dict) -> str | None:
    other = raw.get("other_fields") or {}
    for key in _AGENT_KEYS:
        value = other.get(key)
        if value and str(value).strip().lower() not in _JUNK_VALUES:
            return str(value).strip()
    return None


def _applicant_name(raw: dict) -> str | None:
    value = (raw.get("other_fields") or {}).get("applicant_name")
    if value and str(value).strip().lower() not in _JUNK_VALUES:
        return str(value).strip()
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize(raw: dict, area_name: str, home: tuple[float, float] | None) -> dict:
    """Convert one raw PlanIt record into our canonical row."""
    other = raw.get("other_fields") or {}
    lat, lon = raw.get("location_y"), raw.get("location_x")
    distance = None
    if home and lat is not None and lon is not None:
        distance = round(haversine_km(home[0], home[1], float(lat), float(lon)), 1)

    return {
        "uid": uid_of(raw),
        "reference": raw.get("reference") or raw.get("uid"),
        "authority": raw.get("area_name"),
        "area_matched": area_name,
        "address": raw.get("address"),
        "postcode": raw.get("postcode"),
        "description": raw.get("description"),
        "app_type": raw.get("app_type"),
        "app_size": raw.get("app_size"),
        "app_state": raw.get("app_state"),
        "start_date": _date_or_none(raw.get("start_date")),
        "decided_date": _date_or_none(raw.get("decided_date")),
        "permission_expires": _date_or_none(other.get("permission_expires_date")),
        "agent_name": _agent_name(raw),
        "applicant_name": _applicant_name(raw),
        "distance_km": distance,
        "lat": lat,
        "lng": lon,
        "council_url": raw.get("url"),
        "docs_url": other.get("docs_url"),
        "planit_url": raw.get("link"),
    }


def _date_or_none(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def matches_filters(row: dict, cfg) -> bool:
    """Client-side filters: app_size and keywords (PlanIt supports neither)."""
    if cfg.app_size and row["app_size"] != cfg.app_size:
        return False
    if cfg.keywords:
        desc = (row.get("description") or "").lower()
        if not any(k.lower() in desc for k in cfg.keywords):
            return False
    return True
