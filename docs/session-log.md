# Session log

Running log of working sessions — what's done, what's live, what's next.
Newest entries at the top. Keep each entry short: date, what changed, state
of the deploy, next step.

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
