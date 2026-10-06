# Session log

Running log of working sessions — what's done, what's live, what's next.
Newest entries at the top. Keep each entry short: date, what changed, state
of the deploy, next step.

## 2026-10-06 — RLS rerun + parent-hunt config failures

- **Project merge by parent_ref + address corroboration** (`projects.merge_linked`,
  called at the end of `group()`): children whose `parent_ref` resolves to an
  application in another project are merged only if addresses corroborate:
  A same postcode, B postcode missing + street words overlap + house numbers
  don't conflict, X2 different postcode but same number and street. Otherwise
  logged once as "cross-reference, not merged" (e.g. agent citing other work).
  Survivor = largest project; absorbed address keys go to new table
  `project_aliases` so regroups land in the survivor.  B&D hunt: 139 found / 5 exhausted / 22 pending.
  Applied result: projects 164,288 -> 162,802 (1,486 absorbed, 1,283 groups,
  largest 8); no orphans/empty projects/duplicate memberships; 113 links left
  as cross-references. Local code, **not yet committed/deployed**; `project_aliases`
  needs RLS added in scripts/rls.sql.
- Fixed `scripts/rls.sql` reruns: drop the existing `authenticated read`
  policies before recreating them on `projects`, `project_applications`,
  and `missing_parents`. The fetcher-table policy loop was already idempotent.
- Fixed worker argument construction for `parent_hunt`: jobs store the
  authority (used for parent resolution), while `--area` expects the matching
  configured area name. Resolve authority to config name before running fetch.
- `apply_rls.py` ran successfully against the DB (anon denied; authenticated
  read-only). Needs Supabase **Session pooler** URL (direct host is IPv6-only).
- Analysis: `projects.last_updated` is Oct 2026 for ~all projects (backfill
  stamped them), so the "last N months" filter in `enqueue_parent_hunt` is a
  no-op: 1 and 9 months both give 542 jobs / 9,229 refs. 2021–2023 hold ~5.6k
  refs. Ideas: use max applications.last_changed as activity signal, skip
  2000–2012, cap jobs/night. **Undecided.**
- Deleted 524 pending parent_hunt jobs (all except Barking and Dagenham, which
  is the trial run). Added `AUTO_PARENT_HUNT` env switch (default off) so the
  worker no longer re-enqueues hunts at startup/nightly. Manual
  `enqueue-parent-hunt` still works.
- Code changes are local and **not deployed**. Until deployed, a worker
  restart WILL re-enqueue ~537 hunts. Next: check Barking and Dagenham
  results, then decide staggered strategy (PlanIt API load is the concern).
- **Hunt rewritten (per-reference lookup)**: old hunts stored everything
  fetched (~10.5k B&D rows flagged as leads). PlanIt supports
  `id_match=<ref>` + `auth=<authority>` (exact, one ref per request; lists,
  pipes, repeated params don't work). `worker.run_parent_hunt` now: one job
  per authority, loops pending `missing_parents` (ref_year not null, newest
  first), calls `api.lookup_reference`, upserts the match with in_leads=False,
  marks `found` (before regrouping), regroups parent + children, marks misses
  `exhausted`, follows new chains. Pace: `HUNT_DELAY_SECONDS` (default 10s).
  `enqueue-parent-hunt` enqueues one job per authority (no --months).
  Live test on 4 B&D refs: 2 found, 2 exhausted; works. ~9.2k refs total.
- **Cleanup applied** (worker stopped): deleted 10,495 B&D apps + state_history,
  10,181 memberships, 266 junk pending refs, 24 old hunt jobs; 42 hunt-target
  rows kept. Open: ~40 flagged leads with decided_date < 2023-10-01 to check.
- Still local/uncommitted; deploy, restart worker, then enqueue-parent-hunt.
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
