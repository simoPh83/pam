# Session log

Running log of working sessions — what's done, what's live, what's next.
Newest entries at the top. Keep each entry short: date, what changed, state
of the deploy, next step.

## 2026-10-06 � Parent hunt rewrite, project merge, RLS

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
