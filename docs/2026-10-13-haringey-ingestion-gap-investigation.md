# Investigation: planningalerts apps missing from PAM (Haringey)

**Status:** RESOLVED (2026-10-14) — mechanism proven live; fix designed.
**Trigger:** user gets daily planningalerts.org.uk emails for their location
(Haringey) and can't find those applications in the pam-client frontend by
address or description. Question: (a) PlanIt broken, or (b) gap in our logic?
**Sample refs:** HGY/2026/2693 (from email received 2026-09-30),
HGY/2025/0047 (second sample from user).

## Verdict so far

**(b) — gap in our logic.** PlanIt has both records (verified by direct
`id_match` lookups). Two separate mechanisms explain the two samples, plus a
third systemic issue discovered along the way.

### 1. HGY/2026/2693 — excluded by design

- PlanIt: `app_type='Trees'`, Undecided, 29 Hornsey Lane Gardens N6 5NY,
  start 2026-09-29.
- `config.yaml` → `exclude_app_types: [Trees]` ("tree works are never
  photography leads"). Dropped at `matches_filters`, never stored. Working as
  configured. (We hold only 2 Trees rows for Haringey.)

### 2. HGY/2025/0047 — retention-prunable (but see #3)

- PlanIt: `Full`, **Undecided**, 35-39 The Broadway N8 8DU,
  start_date 2025-01-22, **last_different 2025-01-23** (data never changed
  since), last_changed/last_scraped 2026-09-18/19 (PlanIt rescrape — likely
  why planningalerts re-emailed it).
- Retention ([pam/retention.py](../pam/retention.py), 16 months, cutoff
  2025-06-13) prunes on `applications.last_updated`, which on insert =
  `min(max(start_date, decided_date), today)` = **2025-01-22** for this app —
  already stale at insert. Even if the backfill fetched it, the next prune
  would delete it (state_history goes with it, so no trace).
- **Open product question:** live *Undecided* applications with no recent
  data-activity get purged by the 16-month rule even though they're still
  live on the council portal and planningalerts keeps surfacing them.
  Decide: exempt Undecided, give them a longer window, or accept.

### 3. SYSTEMIC: the 3-year single-window backfill skips ~half the records

This is the big one. Retention does **not** explain it (user's hypothesis —
checked and ruled out as primary cause):

- Haringey backfill job **#563** (2023-10-01 → 2026-10-01, one window) ran
  2026-10-04: `fetched=8001`, paged to **offset 10,300**, marked `done=True`.
- PlanIt's own `total` for that exact window (verified 2026-10-13): **10,251**.
- Distinct Haringey rows in `applications`: **4,333**.
- June 2024: PlanIt `total=252` vs **13 stored**. Full 2024: 226 stored vs
  ~3,000 implied.
- **Retention-free proof (month probes):** Jan-2025: 245 vs 33 (13%);
  Jun-2025: 299 vs 221 (74%); Jun-2026: 307 vs 238 (78%). Jun-2026 is
  entirely inside the retention window and months after the backfill —
  22% still missing ⇒ skipping proven, retention excluded.
- Retention timeline rules it out as the cause: retention job added
  2026-10-07 (commit `b00fb6e`), app-level pruning only from 2026-10-09
  (commit `09c269c`); global app-prunes 10-13 Oct total only ~3,500 across
  ALL 32 boroughs — nowhere near the ~5,000+ Haringey shortfall. The backfill
  simply never inserted those rows.
- Mechanism (inferred): offset-paging 10k records over a ~2h crawl; PlanIt's
  ordering drifts as records are rescraped, so pages overlap heavily —
  `fetched` counts duplicates, whole date ranges never appear, and upsert
  `ON CONFLICT(uid)` silently absorbs the dupes. Loop legitimately reached
  offset ≥ total, so no error surfaced.
- **Why it never heals:** daily syncs use `different_start` (records whose
  data *changed* recently). An old record that never changes is never
  re-offered by PlanIt in sync mode. Missed by the backfill = missed forever.
- All 32 boroughs show the same collapsed 2023/2024 cohorts (e.g. Westminster
  backfill fetched 21,625 but stores 2023:51, 2024:69 vs 2025:5,247) —
  every borough needs the same fix, not just Haringey.

## Resolution (2026-10-14): mechanism proven, fix designed

### Live probe verdict (scripts/planit_pagination_probe.py, 7 requests, 2026-10-14)

Probed a tiny stable window (Haringey 2026-06-01→02, `total=34`, pg_sz 5):

- **`offset` works** and is interchangeable with the documented `index` /
  `page` params (identical results, `from`/`to` echo correctly). Not the bug.
- **Adjacent pages fetched ~2 min apart OVERLAP**: page 1 = […1538, 1537,
  1539, 1545, 1553], page 2 (offset=5) = [1542, **1539**, 1084, **1545**, 1543].
  2 of 5 records duplicated across consecutive pages of a 34-record set that
  did not change (`total` constant, same records). Duplicates at one end of a
  page boundary imply skips elsewhere — this is the smoking gun.
- **Root cause:** PlanIt's documented default order is `-start_date`. A borough
  registers ~10–30 apps/day, so a 3-year window is thousands of records sharing
  ~1,000 distinct sort keys. Tie order is **not stable across queries** (no
  unique tiebreaker — classic OFFSET-over-non-unique-ORDER-BY). Every page is a
  fresh query 60+s apart; each execution reshuffles the ties. Over 103 pages
  (Haringey backfill: offset 0→10,300) roughly half the fetched records were
  re-reads and ~45% of distinct records were never seen (fetched=8,001 incl.
  dupes vs ~8,200 non-Trees expected; 4,333 stored). The loop terminated
  "cleanly" because offsets kept returning full pages until past `total` —
  `ON CONFLICT(uid)` upserts silently absorbed the dupes.
- **Secondary hazards in the old loop** (not the cause here, but live):
  any flaky short page ends the window early (`len(records) < page_size`
  ⇒ done), and the response meta (`total`, `from`, `to`) was ignored.
- **Fix availability proven:** `sort=start_date.asc,name.asc` is accepted and
  yields a canonical, unique, immutable order (page 1 = 0878, 1182, 1195, 1406,
  1407 — uid-ordered within the day). `total` is present on every response.

### The fetch fix (to implement)

1. `pam/api.py fetch_area`: always send `sort=start_date.asc,name.asc`
   (unique + immutable ⇒ deterministic paging when membership is stable).
2. Page by server-reported positions: stop when `to >= total - 1`; next page
   starts at `to + 1` (self-heals short pages; no premature termination).
3. Reconcile per window: distinct uids yielded vs `total`; on mismatch
   re-scan the window once, then flag `reconcile=failed` in fetch_progress +
   job stats instead of silently marking done.
4. Chunk re-backfills into monthly windows
   (`worker enqueue-backfill --chunk month`): each window gets its own
   fetch_progress key; small windows minimise exposure to mid-crawl membership
   changes (PlanIt ingesting old records) and to 429 pauses.
   Budget: 33 boroughs × 36 months × ~3 pages ≈ 3,500 requests ≈ ~12 nights
   under the 300/day cap + 60s pacing.
5. Daily syncs (`different_start`): same sort + advisory reconcile; the
   next-day watermark overlap self-heals residual misses.

Deploy sequencing per playbook: stop worker → purge (see filtering design) →
re-backfill in monthly chunks → restart worker. Ingest filtering must land
BEFORE the re-backfill or it re-adds the minor works.

## Topic B: minor-works filter + purge (2026-10-14)

Full analysis + design: [2026-10-14-minor-works-filter-design.md](2026-10-14-minor-works-filter-design.md).
Headline: 62.5% of projects (70,531, holding 79,655 applications) classify as
minor by their MAIN application's description ⇒ ~171 MB net reclaim;
51.3% of records would be blocked at ingest; 8,055 projects lack their main
application (postpone set — never auto-purged).

## Evidence log (queries used)

- DB: `applications` exact-ref lookup → 0 rows for both refs; Haringey
  total 4,333; year distribution 2023:47 / 2024:226 / 2025:2,022 / 2026:2,006.
- `jobs` table: Haringey = 1 backfill (#563, 2023-10-01→2026-10-01, done,
  stats fetched 8001) + daily syncs since 2026-10-02 (all done, small counts).
- `fetch_progress`: backfill window `2023-10-01_2026-10-01`, state `All`,
  last_offset **10300**, done=True.
- PlanIt direct calls (9 total, all single-page, via `lookup_reference` /
  `_get` with `PLANIT_WINDOW_START=0 PLANIT_WINDOW_END=24` override):
  id_match ×2 (both refs found), June-2024 window (total 252), full backfill
  window (total 10,251), month probes Jan-2025 / Jun-2025 / Jun-2026
  (totals 245 / 299 / 307 vs stored 33 / 221 / 238).
- Prune history from job stats: app-level prunes only appear from 2026-10-10
  onward; largest single-night global total ~1,860 apps (2026-10-13) across
  all boroughs — too small and too late to explain the Haringey 2024 hole.
- Retention commits: `b00fb6e` (2026-10-07 "added retention job"),
  `09c269c` (2026-10-09 "Prune member applications with their project").





USER: So for what I understand the paging mechanism on planIt is not reliable and we haven't fetched during our big initial backfill all the applications actually available on that period, but a portion of it.
Now: fixing this with the current filtering logics (filtering out "trees" basically, only) would result in the db getting out of control in terms of size. I would like to introduce a new filtering system that would purge application (actually projects, so root + children applications) based on some sort of parsing over the root description.
So two main topics
A) make sure we got the pagination issue right and we are sure about how to fix the existing logics to be able to scan all applications given a certain time window
B) analyse currently available main applications (where the most accurate description of a project is) and try to create an algorithm for applications that would not be useful for our goal (finding substantial architectural projects, either new builds or conversions, but let's analyse the data together to understand what we can safetly purge or never register, and what needs to be safe.) The db is already about 380mb and I want to stay below the 500mb supabase threshold.
