# Project Brief: Planning Application Monitor (PlanIt API)

## Purpose

A small script that queries the PlanIt public API on a schedule, filters for planning applications relevant to outreach, and builds up a running spreadsheet of leads (architecture practices) without duplicating entries between runs.

### Background

I'm an architectural photographer based in London. As a marketing tool I'm planning to start monitoring planning applications from architects to be able to guess when a project is close to completion, hence reach out to the practice and offer my services. I offer photography for portfolio, press and awards submissions. My website if that's useful is [simonemorciano.com](https://simonemorciano.com).

## 1. Data source

- **Endpoint:** `https://www.planit.org.uk/api/applics/json` (or `/geojson` if you ever want mapping)
- No API key needed; keep requests paced (it's a free, volunteer-run service)

## 2. User-configurable parameters

Expose these as either command-line flags or a small config file (YAML/JSON) so you can tweak a run without touching code:

| Parameter | Description |
|---|---|
| `app_state` | e.g. `Permitted` (default), but leave it possible to include `Undecided`, `Withdrawn`, etc. |
| `start_date` / `end_date` | Date range for `decided_date` (or `start_date` if you want to catch things earlier in the pipeline) |
| `area` | Postcode + radius, authority name, or bounding box (whatever matches how you think about "my area") |
| `app_size` | Optional filter (Small/Medium/Large), useful if you only care about certain project scales |
| `keywords` | Optional free-text filter on the description field |

## 3. Output fields (one row per application)

- `reference`, `authority_name`, `address`, `postcode`
- `description`, `app_type`, `app_size`, `app_state`
- `start_date`, `decided_date`
- **Agent/practice name** — present on many but not all records, field naming varies a bit by council system
- **link/url** — direct link back to the council's own record

> **Note on "website":** PlanIt's structured feed reliably gives you the agent/practice name far more often than contact details like email, phone, or website — those depend entirely on what the individual council chose to publish, and there's no guaranteed field for them. Treat "website" as a manual/secondary lookup step for now (search the practice name), rather than something the script can fill in automatically. Could be a v2 enrichment step later.

## 4. Deduplication / merge logic across runs

- Pick a stable unique key per application — `uid` (if returned) or a composite of `authority_id` + `reference`
- Keep a small local ledger (SQLite table or a simple CSV/JSON of seen keys) alongside your output spreadsheet
- On each run:
  1. Fetch results matching current filters
  2. Skip any key already in the ledger
  3. Append genuinely new rows to the output spreadsheet
- **Optional nice-to-have:** if a previously-seen application's `app_state` has changed (e.g. `Undecided` → `Permitted`), update that row instead of skipping it — this is often the actual trigger moment you care about

## 5. Suggested implementation

- **Python** (`requests` + `csv`/`openpyxl`, or `pandas` if you want it dead simple) — quick to write, easy to schedule via cron or Task Scheduler
- Output straight to a `.csv` (opens fine in Excel/Sheets) or `.xlsx` if you want formatting
- Ledger as a lightweight SQLite file or even a second CSV of just the keys — no need for a real database at this scale

## 6. Known limitations

- Coverage and freshness depend on each council keeping PlanIt's crawl up to date — some lag
- This tracks planning *decisions*, not construction completion — you'll be reaching out somewhat earlier than "the building is finished," which is a feature more than a bug for early outreach, but worth knowing going in

## Appendix: verified API findings (2026-09-27, live probes against the API)

These were confirmed by test queries and override assumptions above where they conflict:

- **`start_date` / `end_date` filter on the application's `start_date` (registration), NOT `decided_date`.** There is no server-side decided-date filter — the script must filter `decided_date` client-side. Backfill and state-transition logic must be designed around this.
- **`app_state` server-side param works** (e.g. `app_state=Permitted` returns only Permitted). Use `pg_sz >= 10`; tiny page sizes returned flaky empty results.
- **Observed `app_state` values:** `Undecided`, `Permitted`, `Conditions`, `Rejected`, `Withdrawn`, and occasional `null`. "Permitted" and "Conditions" are both approvals — a permission "granted with conditions" lands in `Conditions`, so leads logic should treat both as positive outcomes.
- **Volume:** ~2,300 applications started within 5 km of N19 4HF in 3 months (of ~500 sampled: 287 Undecided, 131 Permitted, 69 Conditions). Ample data; pagination and pacing are required.
- **Unique key:** use `name` (composite `AreaName/uid`, e.g. `Islington/P031722`) or `area_id` + `uid`. Top-level `reference` is often `null`.
- **Location:** every record has `location_x`/`location_y` (lon/lat) and `location` GeoJSON — distance from home/office is a local haversine calculation, no extra API calls.
- **Links:** `link` = PlanIt detail page; `url` = council's own record; `other_fields.docs_url` = council document repository.
- **Agent name is the weak point (confirmed):** Islington exposes `other_fields.agent_name` but filled with the junk value `"See source"`; Camden has no `agent_name` key at all (only `applicant_name` in ~83% of records). The PlanIt detail page does not reliably show agent info either. Usable agent/practice names will require scraping individual council portals (per-council work) — firmly a **v2 enrichment step**, as the brief suspected.
- **Pacing:** the API is slow (7–30 s per page) and volunteer-run — one request at a time, ~2 s delay between pages, generous timeouts (90–120 s), retry with backoff.
