# API Comparison: PlanIt vs planning.data.gov.uk

**Date:** 2026-09-27
**Purpose:** decide which open API should power the Planning Application Monitor for architectural-photography lead generation in London (home base N19 4HF).
**Method:** documentation review + live probe queries against both APIs on the same day.

---

## Verdict

**Use PlanIt.** planning.data.gov.uk's `planning-application` dataset is in alpha, effectively unpopulated for London, frozen since mid-2025, and lacks decision outcomes, geometry, and agent names. Revisit in ~12 months — the platform itself is well-built and government-backed, and LPAs may eventually be required to publish to it.

## 1. planning.data.gov.uk — verified findings

**Platform:** Ministry of Housing, Communities and Local Government (MHCLG). Beta service, Open Government Licence v3. API docs at [planning.data.gov.uk/docs](https://www.planning.data.gov.uk/docs). 100+ datasets (conservation areas, listed buildings, brownfield land, flood risk…) through one consistent `GET /entity.{json|geojson}` interface, with OpenAPI spec and bulk CSV/GeoJSON/Parquet downloads.

**API quality (genuinely good):**

- Fast responses (vs PlanIt's 7–30 s per page)
- Proper pagination: `limit`/`offset` plus pre-built `next`/`prev`/`first`/`last` links
- Spatial queries: point (`latitude`/`longitude`), WKT polygon (`geometry`), LPA boundary (`geometry_entity`) with relations (`within`, `intersects`, …)
- Date filters: `start_date_*`, `end_date_*`, `entry_date_*` with `match`/`before`/`since`
- Field selection (`field`/`exclude_field`) to slim responses
- Polite use requested; no key required

**`planning-application` dataset reality (probed live):**

| Check | Result |
|---|---|
| Total records | 100,627 |
| Contributing LPAs in a 500-record sample | **1 only** (organisation-entity 109 — legacy pilot batch, decisions 2023-01 → 2024-11, all entered 2025-05 → 2025-06) |
| Applications within 5 km of N19 4HF since 2026-06-27 | **0** |
| Records with `entry-date` since 2026-09 | **0** |
| Records with geometry/point (of 500 sampled) | **0** — spatial queries return nothing for applications |
| Decision outcome field (Permitted/Refused) | **Absent** — schema has `decision-date` but no decision/status field |
| Agent/practice name | **Absent** |
| Applicant name | Absent |
| Dataset phase | `alpha` |

**Record schema (all fields):** `entity`, `reference` (e.g. `23/00002/FUL`), `name`, `description`, `dataset`, `typology`, `prefix`, `organisation-entity`, `start-date`, `end-date`, `entry-date`, `decision-date`, `geometry`, `point`, `quality`.

**The docs themselves warn:** *"the planning applications data specification is currently in development and may change. Local planning authorities are not currently required to share application data to this specification."*

**Where it IS strong today:** statutory constraint datasets (listed buildings with grades, conservation areas, Article 4 directions, flood zones) are well populated — potentially useful for a future enrichment pass (e.g. flagging leads in conservation areas), but not for lead generation itself.

## 2. Head-to-head

| Criterion | PlanIt | planning.data.gov.uk |
|---|---|---|
| London coverage | ✅ All boroughs crawled | ❌ None on-boarded |
| Decision outcome (Permitted/Refused) | ✅ `app_state` (Permitted, Conditions, Undecided, Rejected, Withdrawn) | ❌ No outcome field at all |
| Per-application geometry | ✅ lon/lat on every record | ❌ Empty in sample |
| Freshness | ✅ Lagged weeks, continuously crawled | ❌ Frozen June 2025 |
| Volume near N19 4HF (3 months) | ✅ ~2,300 applications | ❌ 0 |
| API speed/ergonomics | 🐌 Slow (7–30 s/page), quirky pagination | ✅ Fast, clean, OpenAPI |
| Agent/practice name | ❌ Junk/absent (Islington "See source", Camden none) | ❌ Absent entirely |
| Historical depth | ✅ Back to early 2000s | ❌ Pilot batch 2023–2024 only |
| Licence / governance | Informal, volunteer-run | ✅ OGL v3, MHCLG |
| Future trajectory | Mature, static | 🌱 Alpha; could become mandatory LPA publishing channel |

## 3. Decision and future-proofing

1. **Build on PlanIt** as the single data source for v1 (see [2026.09.27-planit-api-findings.md](2026.09.27-planit-api-findings.md)).
2. **Design seam:** keep the API client behind a small interface — `fetch(filters) -> normalized records` — so planning.data.gov.uk (or council-specific feeds) can be added as additional sources later without touching ledger/spreadsheet logic.
3. **Re-check planning.data.gov.uk in ~12 months** (or on any MHCLG announcement making the planning-application spec mandatory). Trigger criteria for a migration or secondary-source integration: London LPAs publishing, a decision-outcome field in the schema, and per-record geometry.
4. **Possible future enrichment from planning.data.gov.uk:** join leads against its constraint datasets (conservation area, listed building proximity) — a genuine differentiator for an architectural photographer's outreach ("I see your project is in the X conservation area…").

---

*Companion docs: [2026.09.27-planit-project-brief.md](2026.09.27-planit-project-brief.md) (intent), [2026.09.27-planit-api-findings.md](2026.09.27-planit-api-findings.md) (PlanIt ground truth).*
