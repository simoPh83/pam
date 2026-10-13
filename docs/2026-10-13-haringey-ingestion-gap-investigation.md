# Investigation: planningalerts apps missing from PAM (Haringey)

**Status:** IN PROGRESS — paused mid-investigation on 2026-10-13 ~10:30. Resume from
"Next steps" below.
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

## Next steps (resume here)

1. ~~Collect the pending probe result~~ — **DONE, conclusive.** PlanIt
   `total` vs stored rows for Haringey:

   | Month | PlanIt | Stored | Capture |
   |---|---|---|---|
   | Jan 2025 | 245 | 33 | 13% |
   | Jun 2025 | 299 | 221 | 74% |
   | Jun 2026 | 307 | 238 | 78% |

   June 2026 is fully inside the 16-month retention window (and post-dates
   the Oct-4 backfill by months, so retention cannot have touched it) — yet
   **22% of it is missing**. Retention is definitively ruled out; the
   backfill paging skip is proven. (If anything the true backfill capture
   rate is worse: some stored 2025/2026 rows arrived via daily syncs, not
   the backfill.)

2. **Decide remediation with user** (offered, not yet answered):
   re-backfill in **monthly windows** per borough
   (`python -m pam.worker enqueue-backfill --from … --to …` — each window
   gets its own `fetch_progress` key; upserts are idempotent). Small windows
   page far more reliably. Haringey-only vs all boroughs — evidence says all.
3. **Harden the fetch loop** ([pam/api.py](../pam/api.py) `fetch_area`):
   - log/record PlanIt's `total` (response meta field) per window and compare
     against distinct-uid count at the end; flag mismatches instead of
     silently marking `done`;
   - treat a short/empty page in a long backfill as suspect (retry once
     before declaring the window finished);
   - count *distinct* uids in run stats, not just `fetched`.
4. **Answer the retention product question** for stale-but-live Undecided
   apps (see #2) — affects whether re-backfilled 2023-24 data would just be
   pruned again. NOTE: re-backfilling stale apps IS safe from immediate
   prune only if their `last_different` is within 16 months; older ones will
   be pruned at the next sync by design. That's the intended slimming, but it
   means "browsable history" for undecideds is capped at 16 months of
   *activity*, not registration.
5. Deploy sequencing per playbook: stop worker → run backfill/sweeps →
   restart worker.

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
