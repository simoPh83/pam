"""Normalize raw PlanIt records into the shape the ledger and spreadsheet use.

Unique key: PlanIt's `name` (e.g. "Islington/P031722"), falling back to
area_id + uid. Top-level `reference` is often null — never use it as a key.
"""

from __future__ import annotations

import math
from datetime import date

# other_fields keys seen holding agent info across councils (mostly junk —
# "See source" — but cheap to check; real values appear in some councils)
_AGENT_NAME_KEYS = ("agent_name", "agent")
_AGENT_COMPANY_KEYS = ("agent_company", "agent_organisation")
_JUNK_VALUES = {"see source", "n/a", "na", "not supplied", "unknown", "-",
                "not in borough"}

# other_fields keys promoted to dedicated ledger columns (schema review
# 2026-10-01). Dates go through _date_or_none; counts through _int_or_none.
_OTHER_DATE_KEYS = ("date_received", "date_validated", "target_decision_date",
                    "consultation_end_date", "application_expires_date",
                    "decision_issued_date", "appeal_date", "appeal_decision_date",
                    "comment_date", "neighbour_consultation_start_date",
                    "neighbour_consultation_end_date", "consultation_start_date",
                    "decision_published_date")
_OTHER_INT_KEYS = ("n_documents", "n_comments", "n_constraints", "n_dwellings",
                   "n_statutory_days")

# Never stored: no value (case_officer is always "See source"; easting/northing
# duplicate lat/lng).
_DROP_KEYS = {"case_officer", "easting", "northing"}


# other_fields key -> (ledger column, kind). A key is dropped from the stored
# "extras" JSON only when the dedicated column already holds the same value.
_EXTRA_KEY_COLUMNS: dict[str, tuple[str, str]] = {
    "source_url": ("source_url", "raw"), "docs_url": ("docs_url", "raw"),
    "comment_url": ("comment_url", "raw"), "map_url": ("map_url", "raw"),
    "ward_name": ("ward", "text"), "status": ("source_status", "text"),
    "decision": ("decision", "text"), "decided_by": ("decided_by", "text"),
    "parish": ("parish", "text"), "development_type": ("development_type", "text"),
    "planning_portal_id": ("planning_portal_id", "text"), "uprn": ("uprn", "text"),
    "appeal_reference": ("appeal_reference", "text"),
    "appeal_result": ("appeal_result", "text"),
    "applicant_name": ("applicant_name", "text"),
    "agent_name": ("agent_name", "text"), "agent": ("agent_name", "text"),
    "agent_company": ("agent_company", "text"),
    "agent_organisation": ("agent_company", "text"),
    "agent_address": ("agent_address", "text"),
    "application_type": ("application_type", "text"),
    "applicant_address": ("applicant_address", "text"),
    "permission_expires_date": ("permission_expires", "date"),
    "decision_date": ("decided_date", "date"),
    "lat": ("lat", "float"), "latitude": ("lat", "float"),
    "lng": ("lng", "float"), "longitude": ("lng", "float"),
    **{k: (k, "date") for k in _OTHER_DATE_KEYS},
    **{k: (k, "int") for k in _OTHER_INT_KEYS},
}


def _comparable(value, kind: str):
    if kind == "date":
        parsed = value if isinstance(value, date) else _date_or_none(value)
        return parsed.isoformat() if parsed else None
    if kind == "int":
        return _int_or_none(value)
    if kind == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if kind == "raw":
        return str(value).strip() or None
    return _clean(value)


def extra_fields(other: dict | None, row: dict) -> dict:
    """Return the other_fields entries NOT already stored in dedicated columns.

    `row` holds the column values (normalized row or a DB row; dates may be
    date objects or ISO strings). Blank/junk values of mapped keys are dropped
    too, since nothing is lost by it.
    """
    extras = {}
    for key, value in (other or {}).items():
        if key in _DROP_KEYS:
            continue
        mapping = _EXTRA_KEY_COLUMNS.get(key)
        if mapping is None:
            extras[key] = value
            continue
        column, kind = mapping
        parsed = _comparable(value, kind)
        if parsed is None:
            if value is None or str(value).strip().lower() in _JUNK_VALUES | {""}:
                continue
            extras[key] = value
            continue
        stored = _comparable(row.get(column), kind)
        same = (abs(parsed - stored) < 1e-6 if kind == "float" and stored is not None
                else parsed == stored)
        if not same:
            extras[key] = value
    return extras


def uid_of(raw: dict) -> str:
    name = raw.get("name")
    if name:
        return str(name)
    return f"{raw.get('area_id')}/{raw.get('uid')}"


def _first_clean(other: dict, keys) -> str | None:
    for key in keys:
        value = _clean(other.get(key))
        if value:
            return value
    return None


def _agent_name(raw: dict) -> str | None:
    return _first_clean(raw.get("other_fields") or {}, _AGENT_NAME_KEYS)


def _agent_company(raw: dict) -> str | None:
    return _first_clean(raw.get("other_fields") or {}, _AGENT_COMPANY_KEYS)


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return None if not value or value.lower() in _JUNK_VALUES else value


def _applicant_name(raw: dict) -> str | None:
    return _clean((raw.get("other_fields") or {}).get("applicant_name"))


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

    row = {
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
        "agent_company": _agent_company(raw),
        "agent_address": _clean(other.get("agent_address")),
        # business if known, else whatever the council supplied
        "agent_display": _agent_company(raw) or _agent_name(raw),
        "applicant_name": _applicant_name(raw),
        "distance_km": distance,
        "lat": lat,
        "lng": lon,
        "council_url": raw.get("url"),
        "docs_url": other.get("docs_url"),
        "planit_url": raw.get("link"),
        # PlanIt change metadata: last_different = when the data last changed
        "last_changed": raw.get("last_changed"),
        "last_different": raw.get("last_different"),
        "last_scraped": raw.get("last_scraped"),
        # --- promoted from other_fields ---
        "decision": _clean(other.get("decision")),
        "decided_by": _clean(other.get("decided_by")),
        "source_status": _clean(other.get("status")),
        "ward": _clean(other.get("ward_name")),
        "parish": _clean(other.get("parish")),
        "development_type": _clean(other.get("development_type")),
        "source_url": other.get("source_url"),
        "comment_url": other.get("comment_url"),
        "map_url": other.get("map_url"),
        "planning_portal_id": _clean(other.get("planning_portal_id")),
        "uprn": _clean(other.get("uprn")),
        "appeal_reference": _clean(other.get("appeal_reference")),
        "appeal_result": _clean(other.get("appeal_result")),
        "application_type": _clean(other.get("application_type")),
        "applicant_address": _clean(other.get("applicant_address")),
    }
    for key in _OTHER_DATE_KEYS:
        row[key] = _date_or_none(other.get(key))
    for key in _OTHER_INT_KEYS:
        row[key] = _int_or_none(other.get(key))
    return row


def _int_or_none(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _date_or_none(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def matches_filters(row: dict, cfg) -> bool:
    """Client-side filters: app_size, keywords, excluded app_types."""
    excluded = getattr(cfg, "exclude_app_types", None) or []
    if row["app_type"] in excluded:
        return False
    if cfg.app_size and row["app_size"] != cfg.app_size:
        return False
    if cfg.keywords:
        desc = (row.get("description") or "").lower()
        if not any(k.lower() in desc for k in cfg.keywords):
            return False
    return True
