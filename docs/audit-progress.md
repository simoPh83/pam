# Borough Audit — Progress Log

Living document tracking which London boroughs have been audited against the
PlanIt API, what data each exposes, and what remains. Updated per session; see
[2026.09.27-borough-audit.csv](2026.09.27-borough-audit.csv) for the raw numbers.

**Tooling:** `python -m pam.audit` ([pam/audit.py](../pam/audit.py)) — fills CSV
rows where `fetched = -1`. Flags: `--refresh` (redo all), `--authority X Y`
(targeted), `--dry-run`, `--days N`, `--areas [--match TEXT]` (area-list cache
and name lookup), `--log FILE` (tee timestamped progress into a committable log).

---

## Backfill — first live run (session 3, evening)

**Goal:** validate the full pipeline (`pam.main`: fetch → ledger → leads.xlsx)
on ONE borough before spending the rate-limit budget on all of them.

**Borough choice: Enfield.** Highest agent-name coverage in the audit (86/100),
fresh crawl (2026-09-26), and never previously fetched by `pam` — so the run
exercises the full backfill path from a cold ledger.

**Setup:**
- [config.yaml](../config.yaml): only `enfield` enabled; the 10 session-1 areas
  commented out (re-enable after this validates). Backfill window 2026-06-27 →
  today (3 months), as configured. States fetched: Permitted + Conditions +
  Undecided (watch). Trees excluded via `exclude_app_types`.
- Command: `.\.venv\Scripts\python.exe -m pam --log logs\2026-09-28-backfill-enfield.log`
  (NOT `--dry-run` — writes `data/leads.xlsx` + `data/ledger.sqlite`).
- Resumability: `fetch_progress` table checkpoints per (window, area, state)
  page — if rate-limited or interrupted, re-running the same command skips
  completed state/area pages.

**What to check after (acceptance criteria):**
1. `data/leads.xlsx` exists; rows are Permitted/Conditions decided within the
   window; agent_name populated on most rows (Enfield ~86%).
2. Ledger row counts: `SELECT app_state, COUNT(*) FROM applications GROUP BY 1`
   — expect a few hundred Undecided tracked for future transitions.
3. Log shows no unhandled errors; any 429s were waited out cleanly.
4. Sanity-check 2–3 rows against their `council_url` — does the agent name
   match the council portal?

**Estimated cost:** Enfield 3-month volume unknown, likely 300–800 records
across 3 states → ~10–30 pages at 100/page → 15–60 min with 429 waits.

**OUTCOME (same evening): ✅ PASSED, no rate-limit waits needed.**

- 820 fetched → **232 leads added** (176 Permitted + 56 Conditions decided
  in-window), 585 Undecided tracked for future transitions, 0 errors.
- **Agent names on 196/232 leads (84%)** — audit prediction (86%) confirmed.
  Real practices: DPP Planning, NIE ASSOCIATES, Praktical Solutions Ltd…
- Artifacts: data/leads.xlsx, data/ledger.sqlite,
  logs/2026-09-28-backfill-enfield.log.
- Invocation notes: main entry is `python -m pam.main` (package has no
  `__main__.py`); `--log` had to be registered in config.py's parser AND
  handled in main.py — both done this session.
- Spot-check of 2–3 rows against council portals still worth doing by eye.

**Next session: expand to the other 7 agent boroughs.** Uncomment/add in
config.yaml areas: greenwich, lambeth, lewisham, newham, southwark, sutton,
tower-hamlets (keep enfield). Run the same command — Enfield's completed
pages are skipped via fetch_progress; each new borough backfills. Expect
roughly 232 × 7 ≈ 1,600 leads total and several hours of wall time (rate
limits); can also be done one borough per sitting via `--area NAME`.

**Greenwich (added same evening, `--area greenwich`): ✅**
- 520 fetched → **133 leads added**, 383 Undecided tracked; no errors, no 429s.
- Agent names on **98/133 leads (74%)** — real practices: Taylor Wimpey London,
  Stantec UK, SAM Planning services, Sphere25, Redwoods Projects.
- ⚠️ **Greenwich has ZERO `Conditions` records** — everything decided lands as
  `Permitted`. Per-council state vocabularies differ (the audit's core theme);
  the config's `lead_states: [Permitted, Conditions]` covers both shapes, but
  remember "no Conditions" ≠ broken when comparing boroughs.
- Running totals: 365 leads (294 with agent, 80%), 968 Undecided tracked.
- gitignore changed: `data/` is now COMMITTED (ledger must survive across
  machines for dedupe/transition detection) — keep the repo private, and
  note binary files dirty the tree on every run.

**Remaining agent boroughs to add:** lambeth, lewisham, newham, southwark,
sutton, tower-hamlets.

---

## Status: 2026-09-28 (session 3) — AUDIT COMPLETE 33/33

Name mismatches resolved from the areas cache ([docs/planit-areas.json](planit-areas.json),
485 areas, full download log in [logs/2026-09-28-areas-download.log](../logs/2026-09-28-areas-download.log)):

| CSV name | PlanIt feed name | Result |
|---|---|---|
| Kensington and Chelsea | `Kensington` | **stale crawl** (last scrape 2026-08-02; areas table max_date 2026-07-27) — 84 records, 0 agents. Moved to the stale watchlist. |
| Kingston upon Thames | `Kingston` | fresh; 100 records, **1 agent** (outlier, not a usable source) |
| Richmond upon Thames | `Richmond` | fresh; 100 records, 0 agents |

Aliases live in `ALIASES` in [pam/audit.py](../pam/audit.py); CSV rows carry
`(as '...')` annotations like the earlier City/Westminster rows.

**Final tally: 8 boroughs with usable agent names** (Enfield, Greenwich, Lambeth,
Lewisham, Newham, Southwark, Sutton, Tower Hamlets), 19 fresh without agents,
6 stale crawls (Barking & Dagenham, Hackney, Harrow, Kensington, Merton,
Waltham Forest).

**Config impact:** the eight agent-name boroughs are the obvious expansion
candidates for [config.yaml](../config.yaml) `areas:` — agent name is the field
the whole project is for. Fresh-no-agent boroughs can still feed the pipeline
(address + decided_date + council URL), with agent lookup as v2 portal scraping.

**Operational note:** the areas download (49 pages) consumed the rate-limit
budget twice over (429 waits of ~24 min and ~60 min mid-run). The downloader
now checkpoints the cache after every page, and `--log` writes straight into
the repo. Plan areas refreshes as standalone runs.

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

### 🕸️ Stale PlanIt crawls (6) — monitor, don't build on

| Borough | Last scrape |
|---|---|
| Barking and Dagenham | 2026-07-06 |
| Hackney | 2026-07-09 |
| Harrow | 2026-07-03 |
| Kensington | 2026-08-02 (found session 3) |
| Merton | 2026-07-05 |
| Waltham Forest | 2026-07-03 |

All ~3 months stale (were presumably dropped from PlanIt's crawl rotation).
Re-check periodically: `--authority Hackney ...` after a few weeks.

### ⏳ Pending — name mismatch (3)

**RESOLVED in session 3** — see top of file. All three used short feed names
(`Kensington`, `Kingston`, `Richmond`); HTTP 400 = unknown auth name.

<details><summary>Original notes</summary>

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

</details>

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
- **PowerShell `>` redirect writes UTF-16LE.** Mixing it with Python's
  `--log` (UTF-8) in one file produced a mixed-encoding log that VS Code
  flagged ("unusual line terminators") and that needed manual repair.
  Rule: always use `--log logs\FILE` for runs you want to commit; never
  append to a `>`-created file. (Fixed logs/2026-09-28-areas-download.log;
  tail reconstructed from the terminal transcript.)
- **Audit accuracy caveat:** `agent_names` counts only non-junk values via
  `_agent_name()`'s strict filter. A borough with a handful of real agents
  among mostly-null rows can show ~0 (Barnet reportedly has some). Treat the
  8 winners as solid, but the 0s as "weak/absent", not proven-absent —
  re-probe individual boroughs before writing them off for v2 scraping.

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
