"""Classify applications/projects as minor works vs substantial schemes.

PAM's product goal: surface substantial architectural projects (new builds,
conversions, major refurbishments) as photography leads. Most planning
records are householder minor works (rear extensions, dormers, replacement
windows), telecoms kit or adverts — never leads, but they dominate storage.

Two-stage rule, deliberately conservative:
  1. KEEP signals fire first: PlanIt app_size Medium/Large, n_dwellings >= 2,
     or a substantial-work phrase (erection/construction of a building,
     N flats/units, multi-unit conversion, change of use to residential,
     major refurbishment). Any keep signal => not minor.
  2. A minor-work bucket (extension/dormer/windows/solar/telecoms/adverts/…)
     must match the description, or the app_type must be categorically minor
     (Advertising/Telecoms/Trees).
  Only (minor hit AND no keep signal) => minor. Anything else stays.

Used in three places:
  - ingest (pam.main.execute): record-level skip for minor originals
  - grouping (pam.projects): project verdict once the main (earliest
    role='original') application's description is known
  - scripts/purge_minor_projects.py: retroactive DB purge

Bucket/keep regexes were tuned against the live corpus — see
scripts/analyze_project_descriptions.py (the eyeball harness) and
docs/2026-10-13-haringey-ingestion-gap-investigation.md.
"""

from __future__ import annotations

import re

MINOR_BUCKETS: dict[str, str] = {
    "telecoms": r"telecommunication|telecoms?\b|\b5g\b|mobile (phone|broadband)|"
                r"monopole|telegraph pole|radio mast|rooftop (apparatus|equipment)|"
                r"mast\b",
    "adverts_signage": r"\badvert|signage|shop ?sign|shopfront signage|fascia sign|"
                       r"totem sign|poster|billboard|\bhoarding\b|"
                       r"illuminated (sign|fascia)|window vinyl",
    "solar_pv": r"solar (panel|pv|photovoltaic|thermal|farm)|photovoltaic",
    "windows_doors": r"replacement (of )?(the )?(existing )?(windows|doors)|"
                     r"windows (and|&) doors|double[- ]glaz|"
                     r"(window|door)(s)? replacement|new (window|door) opening|"
                     r"\bwindows\b.*\breplac|\bUPVC\b",
    "drop_kerb_access": r"drop(ped)? kerb|vehicle crossover|vehicular (access|crossover)|"
                        r"\bcrossover\b|hardstanding|hard standing|driveway|"
                        r"new (vehicular )?access",
    "satellite_antenna": r"satellite|antenna|aerial\b|\bdish\b",
    "plant_hvac": r"air[- ]?condition|condenser|extract(or|ion) (fan|unit|system|duct)|"
                  r"\bflue\b|ventilation (system|duct|unit)|\bhvac\b|heat pump|"
                  r"plant room|cooling (unit|system)|kitchen extract|"
                  r"\bchiller\b|air source",
    "scaffold_temp": r"scaffold|\bcrane\b|temporary (structure|works|use)|"
                     r"site cabin|welfare (unit|facility)",
    "fence_wall_gate": r"\bfence|\bfencing|\bgates?\b|boundary (wall|fence)|"
                       r"garden wall|retaining wall|\brailings?\b",
    "bins_cycles": r"bin (store|storage)|cycle (store|storage|parking|shed)|"
                   r"refuse (store|storage)",
    "ev_charging": r"\bev charg|electric vehicle|charging (point|station)",
    "internal_only": r"^internal (alterations|works|refurbishment|fit[- ]?out)|"
                     r"internal (alterations|works) only",
    # Rooflight/skylight-only installs. 'loft conversion' / 'dormer' are KEPT
    # (owner, 2026-10-14: loft conversions are wanted leads, like extensions).
    "rooflights_only": r"roof ?lights?\b|skylight",
    "basement": r"\bbasement\b",
    "roof_works": r"re[- ]?roof|roof (repair|replacement|covering)|"
                  r"\bchimney\b|roof works",
    "garden_minor": r"\bsheds?\b|\bdecking\b|\bpergolas?\b|\bcarports?\b|"
                    r"\bsummer ?house\b",
}

# app_types that are categorically never leads (description-independent,
# NO keep-signal rescue — per product owner 2026-10-14: 'the word "erection"
# rescues anything EXCEPT clearly excluded PlanIt groups like Advertising /
# Telecoms / Trees').
PURGE_APP_TYPES = ("Advertising", "Telecoms", "Trees")

# Extensions are valuable leads (product owner, 2026-10-14 — "one of my best
# jobs was an office built on the client's garden"): NO extension/outbuilding/
# porch/conservatory bucket exists, and 'extension' is a KEEP signal so that
# extension projects mentioning rooflights/windows/solar ancillaries are
# never purged for those mentions.

# Structures that stay minor even when 'erected'/'constructed' — curated from
# a corpus tally (2026-10-14): objects of 'erection of' are overwhelmingly
# real buildings/extensions (fences/signs/masts are usually 'installed'), so
# only a few hundred records hit this exception list. Deliberately EXCLUDES
# outbuilding/garage/porch/conservatory (garden-room jobs are wanted).
_MINOR_OBJECTS = (r"fences?|gates?|walls?|hoardings?|signs?|signage|masts?|"
                  r"monopoles?|antennas?|aerials?|dish(?:es)?|railings?|"
                  r"scaffold\w*|sheds?|bin stores?|cycle stores?|substations?|"
                  r"kiosks?|bollards?|palisade|decking|pergolas?|carports?")

# Long descriptions describe multi-part schemes; a length veto protects them
# from ancillary minor mentions (owner request 2026-10-14). Veto does not
# apply when ALL hits are pure utility content (a 3.4k-char telecom
# notification is still telecom).
LENGTH_VETO = 750
_HARD_UTILITY = {"telecoms", "satellite_antenna", "adverts_signage",
                 "scaffold_temp"}

# Keep signals require a building/dwelling object or a unit count, so that
# householder phrasing ("Erection of a single storey rear extension") does
# NOT rescue a minor work. Word-bounded: 'outbuilding' must not fire
# 'building'; bare 'flat' excluded ('flat roof' false positive).
_BUILDING_OBJ = (r"\b(?:buildings?|block of flats|dwellings?|houses?|"
                 r"apartments?|bungalows?|hostels?|hotels?|care homes?|"
                 r"schools?|churches?|warehouses?|office blocks?)\b")
KEEP_SIGNALS: dict[str, str] = {
    # 'Erection/construction of <anything>' rescues a project (product owner,
    # 2026-10-14) — unless the erected object within ~35 chars is itself a
    # minor structure (fence, sign, mast, …). The window ignores '.' because
    # minor objects are often dimensioned ("a 1.8m high fence").
    "erection": rf"\b(?:erection|construction) of\b(?![\s\S]{{0,35}}(?:{_MINOR_OBJECTS})\b)",
    # extensions are wanted work; the word also rescues extension projects
    # whose ancillaries (rooflights, windows, solar) hit minor buckets
    "extension": r"\bextensions?\b",
    # likewise loft conversions / dormers (owner, 2026-10-14): only
    # rooflight/skylight-ONLY jobs purge
    "loft_conversion": r"\bloft conversions?|dormer(s)?\b",
    "new_build": r"\bnew[- ]build\b|\bredevelopment\b|\bmixed[- ]use\b|"
                 r"\b\d+\s*(?:no\.?\s*)?(?:[-x]\s*)?(flats?|dwellings?|units|"
                 r"houses|homes|apartments?|bedrooms?|properties)\b|"
                 r"apart-?hotel|student (accommodation|flats)|care home|"
                 r"\bhostel\b",
    "demolition_rebuild": rf"\bdemolition\b[^.;]{{0,120}}\b(erection|construction) of\b[^.;]{{0,40}}{_BUILDING_OBJ}|"
                          rf"\b(erection|construction) of\b[^.;]{{0,40}}{_BUILDING_OBJ}[^.;]{{0,120}}\bdemolition\b|"
                          rf"\bdemolition\b[^.;]{{0,120}}\breplacement (?:with|by)\b[^.;]{{0,50}}{_BUILDING_OBJ}",
    "conversion_multi": r"conversion [^.;]{0,60}(into|to) [^.;]{0,40}\b(flats|dwellings|"
                        r"apartments|properties|\d+\s*(?:no\.?\s*)?units|hmo|"
                        r"house in multiple)\b|"
                        r"convert [^.;]{0,60}(into|to) [^.;]{0,40}\b(flats|dwellings|"
                        r"properties)\b",
    "change_of_use_resi": r"change of use[^.;]{0,80}\b(?:to|into)\b[^.;]{0,50}"
                          r"(residential|dwelling|flat|apartment|hotel|hmo|"
                          r"house in multiple|resi\b)",
    "major_refurb": r"\b(major|substantial|comprehensive|full)[- ](refurbishment|"
                    r"renovation|remodelling)\b|whole[- ]building refurbishment",
}

_MINOR = {k: re.compile(v, re.I) for k, v in MINOR_BUCKETS.items()}
_KEEP = {k: re.compile(v, re.I) for k, v in KEEP_SIGNALS.items()}


def minor_hits(description: str | None, app_type: str | None = None) -> list[str]:
    """Minor-work buckets matched (incl. categorical app_type). Empty = no
    minor suspicion."""
    d = description or ""
    hits = [k for k, rx in _MINOR.items() if rx.search(d)]
    if app_type in PURGE_APP_TYPES:
        hits.append("app_type:" + app_type)
    return hits


def keep_reasons(description: str | None, app_size: str | None = None,
                 n_dwellings: int | None = None,
                 hits: list[str] | None = None) -> list[str]:
    """Substantial-work signals. Non-empty => never purge."""
    reasons = []
    if app_size in ("Medium", "Large"):
        reasons.append("size_med_large")
    if n_dwellings is not None and n_dwellings >= 2:
        reasons.append("n_dwellings_2plus")
    d = description or ""
    reasons += [k for k, rx in _KEEP.items() if rx.search(d)]
    if (len(d) >= LENGTH_VETO and hits is not None
            and not set(hits) <= _HARD_UTILITY):
        reasons.append("long_description")
    return reasons


def is_minor(description: str | None, app_type: str | None = None,
             app_size: str | None = None,
             n_dwellings: int | None = None) -> bool:
    """True only when the record is safely minor.

    Categorical app_types (Advertising/Telecoms/Trees) are minor regardless of
    description or keep signals. Everything else needs a minor bucket hit AND
    zero keep signals."""
    if app_type in PURGE_APP_TYPES:
        return True
    d = description or ""
    hits = [k for k, rx in _MINOR.items() if rx.search(d)]
    return bool(hits) and not keep_reasons(description, app_size, n_dwellings,
                                           hits)
