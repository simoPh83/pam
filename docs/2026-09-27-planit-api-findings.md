# PlanIt API — Verified Findings

**Date:** 2026-09-27
**Method:** live probe queries against `https://www.planit.org.uk/api/applics/json` (no API key), centered on home postcode N19 4HF (Islington, London) plus a Camden cross-check.

---

## 1. Endpoint behaviour

| Aspect | Finding |
|---|---|
| Endpoint | `https://www.planit.org.uk/api/applics/json` (also `/geojson`) |
| Auth | None required |
| Speed | **Slow** — 7–30 s per page; volunteer-run service |
| Reliability | One read timeout on first attempt; retries needed |
| `pg_sz` (page size) | Use **≥ 10** — tiny page sizes (2–3) returned flaky empty results |
| Pagination | Paged responses; full pagination loop required for backfills |

**Implication:** one request at a time, ~2 s delay between pages, generous timeouts (90–120 s), retry with backoff. A 3-month backfill is several paged requests — fine for a scheduled script, too slow for interactive use.

## 2. Date filtering — important gotcha

`start_date` / `end_date` query params filter on the application's **`start_date` (registration date), NOT `decided_date`**.

Verified by probing a known 2003 record: querying `start_date=2003-09-01&end_date=2003-10-01` returned records *started* in that window whose `decided_date` fell in Oct–Nov 2003.

There is **no server-side decided-date filter**.

**Implications:**
- "What was decided recently?" must be computed client-side by filtering `decided_date` from results.
- The daily-run strategy becomes: query a rolling window of recent `start_date`s, track everything (especially `Undecided`) in the ledger, and treat an application's state flipping to `Permitted`/`Conditions` as the outreach trigger. State-change detection is the **core mechanism**, not a nice-to-have.
- Backfill logic must query by start-date window, then filter/group by `decided_date` locally.

## 3. `app_state` filter and observed values

The server-side `app_state` param **works** (`app_state=Permitted` returned only Permitted records — verified).

Observed values in a 500-record sample (3 months, 5 km around N19 4HF):

| State | Count | Meaning for leads |
|---|---|---|
| `Undecided` | 287 | Pipeline — track for future transitions |
| `Permitted` | 131 | ✅ Lead (approval) |
| `Conditions` | 69 | ✅ Lead (approval granted with conditions) |
| `Rejected` | 9 | Not a lead |
| `Withdrawn` | 3 | Not a lead |
| `null` | 1 | Edge case — handle gracefully |

**Implication:** "approved" = `Permitted` **or** `Conditions`. Filtering only on `Permitted` misses ~1/3 of approvals.

## 4. Volume estimate (backfill sizing)

- **~2,300** applications started within 5 km of N19 4HF in the last 3 months (all states).
- Sample composition: ~57% Undecided, ~26% Permitted, ~14% Conditions.
- `app_size` distribution in sample: 480 Small / 18 Medium / 2 Large.
- ~41% of records have a `decided_date` at all.

**Implication:** ample data. The 3-month backfill will produce roughly 200 approved leads within 5 km. `app_size` filtering is worth keeping configurable — Medium/Large projects are rare but likely higher-value.

## 5. Record schema (verified from live records)

Top-level fields present on records:

- `name` — composite key, e.g. `Islington/P031722` (**use as unique key**, with `area_id` + `uid` as fallback)
- `reference` — **often `null`**, do not use as key
- `uid` — e.g. `P031722`
- `address`, `postcode`
- `description`, `app_type` (e.g. `Full`), `app_size` (Small/Medium/Large), `app_state`
- `start_date`, `decided_date` (null until decided), `consulted_date`
- `area_id`, `area_name` (authority)
- `location_x` / `location_y` (lon/lat) and `location` (GeoJSON Point) — **present on every record**
- `distance` — metres (?) from query postcode when `pcode` used (tiny values observed, units unverified)
- `link` — PlanIt detail page
- `url` — **council's own record page** (the most useful outward link)
- `last_changed`, `last_different`, `last_scraped` — PlanIt crawl metadata
- `other_fields` — council-specific grab-bag (varies by authority)

**Implication for distance-from-home:** pure local haversine on `location_x`/`location_y` — no extra API calls. Home postcode N19 4HF geocoded once via postcodes.io.

## 6. Agent/practice name — the weak point (confirmed)

| Source | Result |
|---|---|
| Islington `other_fields.agent_name` | Key exists, but filled with junk value `"See source"` (0 usable in 500-record sample) |
| Camden `other_fields` | No `agent_name` key at all; `applicant_name` on ~83% of records (applicant ≠ agent, often the homeowner) |
| PlanIt detail page HTML | No reliably parseable agent info |

Camden `other_fields` keys (200-record scan): `application_type`, `case_officer`, `date_received`, `date_validated`, `docs_url`, `source_url`, `status`, `target_decision_date`, `comment_url`, `ward_name`, `n_documents`, `applicant_name`, geo fields… — no agent.

**Implication:** the brief's suspicion is confirmed. Usable agent/practice names require scraping individual council portals (per-council work) — firmly a **v2 enrichment step**. v1 output keeps an `agent_name` column (best-effort, often blank) plus `applicant_name` where available, and the `url`/`docs_url` links for manual lookup.

## 7. Decisions locked in so far

- **Config:** YAML file for persistent settings + CLI flags that override per run
- **Multi-area:** config holds a list of areas (postcode+radius and/or authority); each run iterates all areas; each lead tagged with matching area; `distance_km` column from N19 4HF
- **Backfill:** 3 months on first run
- **Approved = Permitted + Conditions**
- **Ledger:** SQLite, keyed on `name`, storing `app_state` for transition detection
- **Output:** XLSX via openpyxl (with CSV export option), state changes update rows in place

---

*Raw assumptions and purpose remain in [2026.09.27-planit-project-brief.md](2026.09.27-planit-project-brief.md); this document is the verified ground truth for implementation.*
