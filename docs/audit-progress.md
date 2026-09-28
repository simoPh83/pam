# Borough Audit — Progress Log

Living document tracking which London boroughs have been audited against the
PlanIt API, what data each exposes, and what remains. Updated per session; see
[2026.09.27-borough-audit.csv](2026.09.27-borough-audit.csv) for the raw numbers.

**Tooling:** `python -m pam.audit` ([pam/audit.py](../pam/audit.py)) — fills CSV
rows where `fetched = -1`. Flags: `--refresh` (redo all), `--authority X Y`
(targeted), `--dry-run`, `--days N`, `--areas [--match TEXT]` (area-list cache
and name lookup).

---

## Status: 2026-09-28 (session 2)

**30/33 boroughs audited. 3 pending name resolution.**

### ✅ Agent names available (the point of the audit)

| Borough | Agents / 100 | Sample value |
|---|---|---|
| Enfield | 86 | Redwoods Projects |
| Lambeth | 83 | Formation design and build |
| Newham | 78 | Devonshires |
| Lewisham | 76 | Treefirst |
| Southwark | 76 | Cut Above Tree Management Ltd |
| Tower Hamlets | 73 | Cowan Architects Ltd |
| Greenwich | 72 | Trees Uk |
| Sutton | 71 | Star Design Solutions Ltd |

⚠️ Several samples are tree/landscaping firms — **the `agent_name` field is
council-agnostic about applicant type**, so leads still need the `app_type !=
Trees` filter (already in config) and probably description keywords to bias
toward architects. Ealing's 56% tree share with 0 agent names suggests those
two things correlate per council.

### ❌ Fresh crawl, no agent names (14)

Barnet, Bexley, Brent, Bromley, Camden, City, Westminster, Croydon, Ealing,
Hammersmith and Fulham, Haringey, Havering, Hillingdon, Hounslow, Islington,
Redbridge, Wandsworth. These need portal scraping (v2) or `applicant_name`
as a weak fallback (only Camden exposes it, ~83%).

### 🕸️ Stale PlanIt crawls (5) — monitor, don't build on

| Borough | Last scrape |
|---|---|
| Barking and Dagenham | 2026-07-06 |
| Hackney | 2026-07-09 |
| Harrow | 2026-07-03 |
| Merton | 2026-07-05 |
| Waltham Forest | 2026-07-03 |

All ~3 months stale (were presumably dropped from PlanIt's crawl rotation).
Re-check periodically: `--authority Hackney ...` after a few weeks.

### ⏳ Pending — name mismatch (3)

`Kensington and Chelsea`, `Kingston upon Thames`, `Richmond upon Thames`
return persistent **HTTP 400** (not 404/429) — feed almost certainly uses a
different name (cf. `City of London (as 'City')`).

**Next step (once rate limit resets):**

```
.\.venv\Scripts\python.exe -m pam.audit --areas
.\.venv\Scripts\python.exe -m pam.audit --areas --match kensing
.\.venv\Scripts\python.exe -m pam.audit --areas --match kingston
.\.venv\Scripts\python.exe -m pam.audit --areas --match richmond
```

Add discovered names to `ALIASES` in [pam/audit.py](../pam/audit.py) (and as
`(as '...')` annotations in the CSV), then `python -m pam.audit` to fill the
rows. Guesses to try: "Royal Borough of Kensington and Chelsea", "Kensington",
"Kingston", "Richmond".

---

## Operational lessons

- **Rate limiting is the binding constraint.** PlanIt 429s hard after ~30–40
  slow requests in an hour; bans observed at ~45 min (2797 s Retry-After).
  A full 33-borough audit (one 100-record page each) takes ~15 min on a good
  run, longer with 429 waits. **Do not** run the `--areas` download (49 pages)
  in the same session as a full audit — do it on its own, it caches for the day.
- **`_get` retry logic works** — the audit survived two 429 storms (394 s and
  227 s waits) and a 403 blip without intervention.
- **`api/areas/json`**: no-param endpoint, all 485 UK areas, paged at 10/page
  (pg_sz capped, `search` param ignored), ~0.04 s/page server-side but the
  `borders` geometry makes pages heavy. Envelope: `{from, to, total, records}`.
  Each record has `area_name`, `long_name`, `scraper_name`, `scraper_type`
  (Idox etc. — v2 portal-scraping intel), `total`, `max_date`, `planning_url`.
- **Cache format gotcha:** `docs/planit-areas.json` holds a plain list; the
  first download attempt wrote a one-page envelope dict there. Loader validates
  `isinstance(data, list)` — fixed in the script.
- **PowerShell:** `... | Select-Object -Last N` buffers all output — looks
  hung on long commands. Prefer redirecting to a file and tailing it.

## Code changes this session

- New [pam/audit.py](../pam/audit.py) + `pam-audit` entry point in
  [pyproject.toml](../pyproject.toml).
- Fixed `Config` dataclass field order in [pam/config.py](../pam/config.py)
  (`exclude_app_types` default broke *all* CLI runs — pre-existing bug).
- `pam.api._get` gained an optional `url` param (areas endpoint reuses the
  same retry/429 handling).

## Open questions for later

1. Do the 3 pending boroughs expose agent names? (Pending name resolution.)
2. Applicant-name coverage beyond Camden — worth a `--refresh` column if we
   ever want it as a fallback ranking.
3. Should `sample_agent` capture *several* values? One example may be
   unrepresentative (e.g. Greenwich's "Trees Uk" hides 71 others).
4. Stale-crawl boroughs: is there a PlanIt contact/issue tracker to report
   dropped scrapers, or a planning.data.gov.uk fallback for those five?
