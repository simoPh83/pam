# Session log

Running log of working sessions — what's done, what's live, what's next.
Newest entries at the top. Keep each entry short: date, what changed, state
of the deploy, next step.

## 2026-10-07 (midday) — spec §2 + §4: practices RLS, alerts module

All 5 spec points now have backend implementations (1 root semantics, 3
parent finder, 5 PlanIt etiquette were already done).

- **§2 practices** — tables are created by the client repo's migrations
  (per the frontend spec); added a guarded, idempotent RLS block to
  [rls.sql](../scripts/rls.sql) as a safety net: `practices` /
  `project_practices` = first user-writable tables (authenticated
  read+write all rows, anon denied, sequence usage granted). No-op until
  the client creates the tables. Re-applied `apply_rls.py` against the live
  DB: block parses, existing policies unchanged.
- **§4 stars + alerts** — new [alerts.py](../pam/alerts.py): after each
  run's regroup, [main.py](../pam/main.py) calls `alerts.generate(conn,
  touched_uids, lead_states)`. For each starred project touched, evaluates
  members against the starring users' enabled rules
  (`nma_filed`/`nma_approved`/`large_approved`/`discharge_decided`; rule
  `app_size` overrides the `Large` default) and inserts into `ui_alerts`,
  idempotent via `unique(user_id, rule_id, uid)`. Ships **dark**: if the
  ui_* tables don't exist yet it logs once and skips — worker never blocks
  the client rollout.
- Deviation from spec (deliberate): triggers fire when the event date is
  within the last 3 days, not only exactly today — PlanIt publishes
  filings/decisions late, and "== today" would miss them; the unique
  constraint keeps re-runs idempotent. `due_on = event_date + after_days`.
- Note: `parent_hunt` jobs don't call `alerts.generate` — hunt-found
  parents are old applications, their events are never recent, so it would
  no-op anyway.
- Not done (spec, deferred): the `alert_sweep` nightly job for rules
  created *after* the triggering event — needs the ui_ tables live first.
- Tested offline: full `_trigger_date` matrix, end-to-end generate() with
  fake conn (multi-user, due-date math, lag window, idempotent insert
  shape), dark mode, empty/no-stars short-circuits.
- **To deploy:** commit + push. Alerts go live automatically once the
  client repo ships its migrations; until then every run logs the skip once.
- Also revised [04-frontend-spec-new-features.md](../web-client-kickoff/04-frontend-spec-new-features.md):
  dropped the full DDL/RLS SQL (client reads the live DB), now lists
  tables/fields per ownership tier; fixed `missing_parents.status` values
  (pending/found/exhausted — spec draft said "fetching"); documented the
  live alert-generation semantics (3-day trigger window, due_on = event +
  after_days, one alert per user/rule/app, starred-only, rules not
  retroactive until the sweep job exists).

## 2026-10-07 — PlanIt etiquette enforced (429 with Retry-After 19134s)

During syncs + hunts, PlanIt escalated from repeated `429 waiting 442s` to a
single `429 waiting 19134s` (~5.3h block). Root cause: we were way over the
etiquette PlanIt publishes (captured 2026-10-05 but never implemented —
"point 5"): sync pages at 5s, hunts at 10s, ~2,800+ requests/night vs the
300/day cap. Full compliance now coded:

- [api.py](../pam/api.py): hard **60s floor** between all requests
  (`_gate()`), **300 requests/day cap** counted per Europe/London day, and
  the **18:00–06:00 Europe/London window** enforced before every request —
  outside it, `_get` raises `PlanItPaused(resume_at)`. On 429/403 the
  request gap doubles (max 8×) for the rest of the process; a Retry-After
  longer than 30 min raises `PlanItPaused` instead of sleeping for hours
  (job requeues, fetch progress is checkpointed, nothing lost). User-Agent
  now carries the contact email (simone.morciano@gmail.com).
- [worker.py](../pam/worker.py): catches `PlanItPaused` → job back to
  `pending` with `not_before = resume_at`, attempt not burned. New
  `api_usage` table + `DbUsageTracker` persist the daily counter across
  redeploys. `HUNT_DELAY` default 10s → 0 (api floor is authoritative);
  `SYNC_HOUR_UTC` default 5 → 18 and sync scheduling is gated on the
  overnight window.
- [config.yaml](../config.yaml): `delay_seconds` 5 → 60.
- requirements.txt: + `tzdata` (zoneinfo on Windows).
- Timezone note: Railway runs in Amsterdam (CEST), PlanIt etiquette is UK
  time — all window logic uses the `Europe/London` tz database, so BST/GMT
  transitions and server location don't matter. Logs print UK local time
  for resume times.
- Tested offline: window edges in BST and GMT, cap raise, 60s floor, usage
  counting, long-Retry-After deferral (no multi-hour sleeps).
- **Consequence:** hunts become a slow trickle — max ~300 API lookups/day
  shared with syncs (sync jobs have higher priority, hunts use the rest).
  The ~9k pending refs will take weeks, not nights; local-first resolution
  (no API call) is unaffected and stays fast. Don't mass-enqueue hunts
  expecting overnight completion anymore.
- **To deploy:** commit + push (Railway redeploys; worker resumes jobs).
  On Railway, clear/ignore `HUNT_DELAY_SECONDS` and `SYNC_HOUR_UTC` env
  vars if set (new defaults are correct). `api_usage` table self-creates on
  worker start.

## 2026-10-06 � Parent hunt rewrite, project merge, RLS

**Evening (~23:45) — TH hunt 0% hit rate investigated, suffix fix:**
Tower Hamlets hunt (job 47961) marked its first ~150 refs exhausted with 0
found. Not a code bug: PlanIt `id_match` is exact on the FULL reference, but
TH descriptions cite refs without the type suffix (PA/26/00475) while PlanIt
stores suffixed names (PA/26/00475/NC) — verified live: bare ref → 0 records,
suffixed → 1; and 'PA/26/00281' → 0 vs 'PA/26/00281/S' → 1. 94% of stored TH
apps carry a suffix; 88% of TH pending refs are suffix-less. Wandsworth/B&D
were unaffected (their refs carry the suffix inline). **Crucially, 162 of the
167 exhausted TH refs (and 88 of the pending) already match an application in
our own DB by exact-or-prefix — no API call was ever needed.** Fixes:
- `projects._resolve_refs`: prefix-aware (`starts_with(ref || '/')`), maps to
  earliest start_date match → stops creating bogus missing_parents rows.
- `projects.merge_linked`: parent join prefix-aware → TH cross-project merges
  now work.
- `worker.run_parent_hunt`: local-first resolution (exact or prefix in our
  DB) — marks found with the existing uid, no API call, no 10s delay; API
  lookup only for refs we don't hold. New `resolved_locally` stat.
- **DONE (~23:55):** user deployed; worker immediately began resolving parents
  (found 0 → 26 within minutes). Flipped ALL 194 TH exhausted rows back to
  pending (168 with local matches resolve free; 26 genuine misses get one
  final API re-check). Running job 47961 picks them up automatically — no
  re-enqueue needed. By ~00:00: pending 696 and draining, found 27,
  re-exhausted 10.
- Note: job `stats` in the jobs table lags during local-resolution streaks
  (flushed every 50 API lookups only; local hits don't increment looked_up).
- TODO (minor): `ref_year_of` extracts bogus years from zero-padded sequence
  numbers — 'PA/15/02027' (a 2015 app) yields 2027 via the '2027' inside
  '02027', so genuinely-old refs sort newest-first in the hunt queue.
  Ordering-only impact; consider word-boundaried year matching.
- Side observation: 4 TH refs have duplicate applications rows with the same
  reference string (e.g. two rows of PA/23/01979/A1) — investigate separately.
- Idea parked: for suffix-less refs with no local match, could try common
  suffixes via API (/NC, /S, /A1) — ~4 calls/ref × hundreds of refs is too
  expensive; only worth it if PlanIt adds fuzzy id_match.

**Evening (~22:15) — disk relief (509 → 455 MB):** DB was over the 500 MB
Supabase cap. Findings: `projects`/`project_applications` are core (leads
view + hunts) — kept; an authority-id lookup would save only ~8–12 MB —
skipped; backfill 3y→2y would save only ~8 MB (data starts Oct 2023) —
skipped. Actions:
- **B&D junk purge**: the 10-05 cleanup had missed 4,672 year-scan rows
  (first_seen 2026-10-05, all decided ≤ 2023-08-10 or NULL). Deleted them +
  memberships + history + 2,551 now-empty B&D projects. The 145 first_seen
  10-06 rows = the 139 hunt-found parents — kept. Side effect: ~3,300
  phantom pre-window B&D rows were showing in the `leads` view (it has no
  date filter) — B&D leads now 116, all legit.
- **state_history slimmed**: 292,250 first-sighting seed rows (99.9% of the
  table, redundant with `applications.first_seen`) deleted; 312 genuine
  change rows kept. [ledger.py](../pam/ledger.py) no longer writes
  first-sighting rows nor reseeds an empty table — **needs commit + push**
  (until deployed, the live worker adds ~200 first-sighting rows/day —
  harmless, re-purge with `DELETE FROM state_history WHERE old_state IS NULL`).
- `VACUUM FULL state_history` + plain `VACUUM ANALYZE` on the big three
  (applications/projects/project_applications too large to VACUUM FULL with
  only ~45 MB headroom; their freed pages will be reused instead of growing
  files). Note: Postgres never shrinks files after DELETE — the Supabase
  disk number only drops on TRUNCATE/VACUUM FULL.
- Indexes kept (postcode/agent_display barely read, ~8.6 MB — revisit later).
- Growth: ~200k apps/yr; plan a retention rule (prune non-lead decided apps
  > 24 months, keep projects/history) within ~2–3 months.

**Evening (~21:30) — hunts enqueued for tonight:** AUTO_PARENT_HUNT confirmed
0 on Railway. Enqueued parent hunts for the top 5 authorities by pending
refs (jobs 47961–47965): Tower Hamlets 683, Barnet 593, Camden 558,
Bromley 520, Southwark 460 = 2,814 lookups, ~8h at HUNT_DELAY 10s, so done
by morning. Worker picked up Tower Hamlets within a minute (running).
Assess results in the morning, then enqueue the next batch (Hackney 455,
Lambeth 444, Hammersmith and Fulham 426, Croydon 421, Richmond 382...).

**Wandsworth hunt review (16:55, job 47960 done, no error):**
- Final: 168 found / 20 exhausted / 2 pending. The 2 pending (`92/C/0446`,
  `W/99/0121`) have no usable year, so the hunt skips them on purpose.
- Worker was restarted by a deploy mid-run; it resumed and finished (last
  run did 8 lookups). Queue is empty now.
- Most "exhausted" refs are not real refs: date strings caught by
  `parent_refs_of` (`12/11/2014`, `DATED04/01/2022`, `28/02/2020`,
  `2021/3601DATED`). A few are real refs PlanIt lacks (`16/05114/FUL`,
  `20/02331/FUL`). Harmless, but TODO: filter date-like strings in
  `parent_refs_of` so they stop creating missing_parents rows.
- Checks clean: no hunt-found parent flagged as lead, no empty projects, no
  duplicate memberships, counts/roots consistent. Cross-project links now
  128 (cross-references left unmerged); project_aliases 1,607; projects 162,720.
- **WARNING / decision needed:** user added the two Railway variables
  (`AUTO_PARENT_HUNT` presumably =1 and `HUNT_DELAY_SECONDS`). If
  `AUTO_PARENT_HUNT=1`, the next 05:00 UTC cycle enqueues hunts for ALL
  remaining authorities (~9,000 refs, ~25h+ at 10s). To stage instead, set it
  back to 0 and enqueue a few authorities at a time via `enqueue()`; or leave
  on if happy with the load on PlanIt. Verify the Railway values first.
**State at end of session (pick up here):**
- Code is committed/pushed by the user (hunt rewrite + merge + RLS files);
  confirm Railway deployed it before relying on any of it.
- **Wandsworth parent_hunt (job 47960) was still running** (checked ~16:35:
  150 looked up, 131 found, 19 not found, 21 pending left). User wants to
  review its result before enqueuing the other authorities. Review checklist:
  found/exhausted split, "cross-reference, not merged" lines in the worker
  log, no hunt rows flagged as leads (`in_leads_sheet` false), no empty
  projects / orphans, new pending refs from chains.
- **Nothing is scheduled nightly for hunts.** `AUTO_PARENT_HUNT` defaults to
  off (code in `maybe_schedule_sync`); only the daily sync is enqueued (from
  05:00 UTC). Check the Railway env var is NOT set to 1. Hunts run only when
  enqueued manually: `python -m pam.worker enqueue-parent-hunt` (one job per
  authority with pending refs; 29 authorities, ~9k refs left, Tower Hamlets
  683, Barnet 593, Camden 558...) or a single authority via `enqueue()`.
  Daily syncs DO group new apps (and add new `missing_parents`), but nothing
  hunts them until a hunt is enqueued.
- Open decisions: pace (`HUNT_DELAY_SECONDS`, default 10s; ~9k lookups is
  ~25h+), whether to add an off-peak window / cap per night, whether to turn
  on `AUTO_PARENT_HUNT` once the hit rate is confirmed (B&D 139 found / 5
  exhausted; Wandsworth ~87% found).
- Open: ~40 `in_leads_sheet=true` apps with decided_date < 2023-10-01 (likely
  hunt-kept B&D parents) to check/unflag; `projects.last_updated` is not a
  usable activity signal (backfill stamped Oct 2026); other pending items from
  earlier logs (practices tables, alerts, PlanIt pacing/User-Agent).

**Done today:**
- Fixed `scripts/rls.sql` reruns (drop policy if exists) and the hunt
  authority-name vs config-slug mismatch in `job_argv`. Added Session pooler
  note: direct Supabase host is IPv6-only, use pooler URL (port 5432).
- **Hunt rewritten (per-reference lookup)**: old hunts stored everything
  fetched. PlanIt `id_match=<ref>` + `auth=<authority>` gives an exact match,
  one ref per request (lists/pipes/repeated params don't work; confirmed in
  docs). `worker.run_parent_hunt`: one job per authority, loops pending
  `missing_parents` (ref_year not null, newest first), `api.lookup_reference`,
  upserts the match with in_leads=False, marks `found` before regrouping,
  regroups parent + children, marks misses `exhausted`, follows chains.
  Job stats: looked_up/found/not_found every 50 lookups.
- **Cleanup applied**: deleted 10,495 B&D apps + state_history, 10,181
  memberships, 266 junk pending refs, 24 old hunt jobs; 42 hunt-target rows kept.
- **Project merge** (`projects.merge_linked`, run at end of `group()`):
  children whose `parent_ref` lives in another project are merged only if
  addresses corroborate (A same postcode; B postcode missing + street words
  overlap + house numbers don't conflict; X2 different postcode but same
  number and street). Else logged once as "cross-reference, not merged".
  Survivor = largest project; absorbed address keys go to `project_aliases`
  so regroups land in the survivor. Applied to DB: projects 164,288 ->
  162,802 (1,486 absorbed, 1,283 groups, largest 8); integrity checks clean;
  ~112 links left as cross-references. Barking Riverside multi-phase sites
  deliberately left unmerged.
- **RLS**: `project_aliases` added; found and fixed `missing_parents` being
  writable by `authenticated` (revoked insert/update/delete). `apply_rls.py`
  now verifies projects, project_applications, missing_parents,
  project_aliases. All tables: anon denied, authenticated read-only.
- B&D hunt result: 139 found / 5 exhausted / 22 pending (chains).
## 2026-10-05 (evening) — root semantics + parent finder (point 1 & 3)

**Done (code + DB, both live):**
- Root detection reworked (`pam/projects.py`): root = earliest member whose
  description references no other application (`has_parent_refs` flag on
  `project_applications`), `role='original'` preferred. Backfill rerun:
  149k projects, 91% with ref-free original roots, 0 is_root mismatches.
- `last_updated` now bumps only when a tracked field actually changes
  (was: every upsert) — makes "updated between W and Z" search meaningful.
- Point 3 implemented: `missing_parents` table (authority, reference,
  ref_year, status pending/found/exhausted), populated from existing data
  (11,029 pending; 9,042 with a plausible year after clamping to
  2000..current+1 — PlanIt's history starts 2000–2002 per authority, so
  pre-2000 refs are unhuntable; 550 junk/old years nulled, leaving **537
  authority-year hunt jobs**). `projects.root_in_db` computed
  (137,521 of 149,548 true). Detection now runs automatically in
  `projects.group()`. Biggest hunt year: 2023 (parents filed just before the
  backfill window opened).
- Worker: new `parent_hunt` job kind (priority 50, year-window fetch per
  authority), `enqueue-parent-hunt --months 9` command, post-job resolution
  marking, and **nightly auto-scheduling** (`maybe_schedule_sync` also
  enqueues hunts; dedupe key carries the hunt round so one retry per window
  is allowed, rounds ≥ 2 are skipped). Scope cap: only projects active in
  last 9 months.
- New script `scripts/find_missing_parents.py` (bootstrap, already run).
- RLS for `missing_parents` appended to `scripts/rls.sql` — **NOT yet applied**.

**Sharing model locked (docs updated):** practices/agent details shared;
stars read-shared/write-own; outreach + alerts strictly per-user private.

**To deploy:** commit + push → Railway redeploy → restart worker.
Then: apply `scripts/rls.sql` (only the new missing_parents block matters),
run `python -m pam.worker enqueue-parent-hunt --months 9`.

**Next:** point 2 (practices tables — schema lives in the client repo),
point 4 (alerts module), point 5 (PlanIt pacing: 60s/300-cap/18:00–06:00
for future jobs; User-Agent with email).

## 2026-10-05 (day) — specs + key decisions

- Wrote `docs/2026.10.05-backend-spec-new-features.md` and
  `web-client-kickoff/04-frontend-spec-new-features.md` from
  `docs/2026.10.05 new features.txt`.
- Decisions: no data deletion for now (revisit at DB size pressure);
  PlanIt etiquette applies to future jobs only; practices = reusable entity.

## 2026-10-04 — type migration + other_fields + project grouping (all live)

- All text columns converted to real types (date/timestamptz/boolean/float/
  jsonb); DB 219 → 175 MB. Worker code is Postgres-only (`DATABASE_URL`
  required).
- `other_fields_json` promoted to real columns (application_type etc.),
  case_officer/easting/northing dropped; JSON now 4 MB.
- Project grouping live: `projects` + `project_applications`,
  `(authority, address_key)` clustering, best-earliest parent_ref,
  `leads` view exposes `project_id`/`n_applications`. Backfilled.
- Docs for the web client in `web-client-kickoff/` (00–04).
