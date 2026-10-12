# Backend spec — new features (2026-10-05)

**Repo:** `simoPh83/pam` — Python worker on Railway + Supabase schema.
**Source requirements:** `docs/2026.10.05 new features.txt`.
**Frontend counterpart:** `web-client-kickoff/04-backend-changes-2026-10-05.md`.

Decisions already made with the owner:
- Retention: **no deletion for now**; revisit when DB size pressures the free
  tier (~250 MB of 500 MB today). A slim `retired_applications` ghost table is
  deferred with it.
- PlanIt etiquette: applies to **future** jobs (nightly sync, parent finder).
  The in-flight 3-year backfill finishes as-is. Start within their limits
  (300 requests/day, 60 s pacing, 18:00–06:00 window); revisit later.
- Practices are a **reusable entity** linked to projects (mini-CRM later).

---

## 1. Root detection — fix `is_root` / `root_uid` semantics

Today `root_uid` = earliest `role='original'` in the cluster (falling back to
earliest member). Requirement: the root is the application whose description
**references no other application** (nothing extracted by `PARENT_RE`) —
the true origin of the project — regardless of date order.

### Changes (`pam/projects.py`)
- In `group()`, compute `has_refs = bool(parent_refs_of(description))` per row
  and carry it into the membership pass.
- Root selection order becomes:
  1. earliest member with `role='original'` AND no extracted refs;
  2. else earliest member with no extracted refs (any role);
  3. else earliest member (current fallback).
- `is_root` follows `root_uid` as today.
- Root selection is computed over **all members of the project**, not only
  the batch being grouped (the aggregate pass already does this — extend its
  `ORDER BY` to use a `has_refs` flag stored on `project_applications`).

### Schema additions
```sql
alter table project_applications
  add column if not exists has_parent_refs boolean not null default false;

alter table projects
  add column if not exists root_in_db boolean;   -- see §3
```
Backfill `has_parent_refs` for existing rows in `scripts/backfill_projects.py`
(idempotent, so just rerun it).

### Per-application display dates
The UI needs, per application: **filed date** and **last update date**.
- Filed = `applications.start_date` (registration) — exists.
- Last update = `applications.last_updated` (date our worker last saw a
  change) — exists, but confirm it reflects *any* field change, not only
  state changes: `Ledger.upsert` currently bumps `last_updated` on every
  upsert. Tighten it to bump only when a tracked field actually changed
  (compare incoming values; skip `last_scraped`/`last_changed` churn) —
  otherwise every nightly sync marks everything as updated and the UI dates
  become meaningless.
- "Root present in DB?" per project = `projects.root_in_db` (§3); the UI
  shows the tick from that, not from inference.

---

## 2. Practices (global, shared metadata)

```sql
create table practices (
  id         bigint generated always as identity primary key,
  name       text not null,
  website    text,
  notes      text,
  created_by uuid references auth.users(id),
  created_at timestamptz not null default now()
);
create unique index on practices (lower(name));   -- dedupe by name

create table project_practices (
  project_id bigint not null references projects(id) on delete cascade,
  practice_id bigint not null references practices(id) on delete cascade,
  notes      text,                       -- project-specific note
  created_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  primary key (project_id, practice_id)
);
```

Notes:
- **Global/shared**: every user sees all practices and links. `created_by` is
  attribution only (same pattern as the upcoming outreach log).
- Contrast with **per-user** metadata (stars, alert rules — §4) which key on
  `auth.users`. Keep this distinction explicit in RLS comments.
- Suggest-only linking from `applications.agent_company` is a UI-side hint
  (match by normalised name); no worker automation in v1.

### RLS (extends `scripts/rls.sql`)
Worker writes are via the `postgres` role (bypasses RLS). Users need
read **and write** here — the first tables where `authenticated` writes:
```sql
alter table practices enable row level security;
alter table project_practices enable row level security;
revoke all on practices from anon;
revoke all on project_practices from anon;
grant select, insert, update, delete on practices to authenticated;
grant select, insert, update, delete on project_practices to authenticated;
create policy "authenticated all" on practices
  for all to authenticated using (true) with check (true);
create policy "authenticated all" on project_practices
  for all to authenticated using (true) with check (true);
-- sequences need usage for identity columns
grant usage on sequence practices_id_seq to authenticated;
```
(Deletion of a practice with links is blocked by FK; UI must unlink first.
Fine for v1.)

---

## 3. Parent finder — `missing_parents` + nightly job

### Table
```sql
create table missing_parents (
  id          bigint generated always as identity primary key,
  authority   text not null,
  reference   text not null,             -- the unresolved parent ref (upper)
  ref_year    smallint,                  -- parsed from the ref, if possible
  requested_by text,                     -- uid of the child application that cited it
  project_id  bigint references projects(id) on delete set null,
  status      text not null default 'pending',  -- pending|fetching|found|exhausted
  attempts    int not null default 0,
  created_at  timestamptz not null default now(),
  found_uid   text,                      -- set when resolved
  unique (authority, reference)
);
```
RLS: same read-only-for-users pattern as `projects` (worker-owned).

### Detection (in `pam/projects.py`, during `group()`)
After resolving refs: any extracted ref with **no match** in `applications`
for that authority gets upserted into `missing_parents` (status pending,
`requested_by` = child uid, `project_id` = child's project). Refs already
`found` are left alone.

Also: `projects.root_in_db` = true iff the project's `root_uid` member has
`has_parent_refs = false` **or** every extracted ref of every member resolves
inside the project. Simpler v1 rule: `root_in_db = (no member cites an
unresolved ref)`. Compute in the aggregate pass:
```sql
root_in_db = not exists (
  select 1 from project_applications pa2
  join missing_parents mp on mp.authority = p.authority
                         and mp.reference = pa2.parent_ref
                         and mp.status <> 'found'
  where pa2.project_id = p.id)
```

### Year parsing
From the ref text: Islington `P2018/2269/FUL` → 2018; Camden `2018/1234/P` →
2018; Tower Hamlets `PA/18/01585` → 2018 (`/NN/` two-digit, pivot < 30 →
20NN); Westminster `18/06246/…` → 2018 (leading two-digit). Bail (null year)
when nothing plausible in 1990–(current year) — those stay `pending` with
`ref_year` null and are fetched only if the authority has few unresolved.

### The fetcher job
New job kind `parent_hunt` in `jobs` (same queue):
1. Nightly (after syncs): pick pending `missing_parents` rows, group by
   `(authority, ref_year)`.
2. For each group, fetch PlanIt window `start_date=YYYY-01-01,
   end_date=YYYY-12-31` for that authority (one paged pass, `app_state=All`),
   upserting everything found through the normal `normalize`/`upsert` path
   (this also groups them, closing the loop).
3. Mark rows whose ref appeared as `found` (set `found_uid`); others stay
   pending with `attempts+1`. After N attempts (start with 2: ref may be
   mid-year-boundary), try the adjacent year once, then `exhausted`.

**Scope cap (the key guardrail):** only hunt parents of applications in
**projects active in the last 9 months** — `projects.last_updated >= today -
270 days`. Measured today: that's essentially everything still meaningful;
the cap exists so we never chase the tail of history. Make the window a
config value (`parent_hunt.active_months: 9`) so we can widen later.

One-off bootstrap: `scripts/find_missing_parents.py` enqueues the initial
batch as `parent_hunt` jobs instead of running inline (respects the queue's
pacing and retry machinery).

---

## 4. Stars + alerts — worker-owned computation

Per-user data (contrasts with §2):

```sql
create table ui_project_stars (          -- project-level, per user
  user_id    uuid not null references auth.users(id),
  project_id bigint not null references projects(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, project_id)
);

create table ui_alert_rules (            -- per user, editable
  id         bigint generated always as identity primary key,
  user_id    uuid not null references auth.users(id),
  kind       text not null,              -- 'nma_filed'|'nma_approved'|
                                         -- 'large_approved'|'discharge_decided'
  after_days int not null default 0,     -- 0 = same day
  app_size   text,                       -- nullable filter, e.g. 'Large'
  enabled    boolean not null default true
);

create table ui_alerts (                 -- generated
  id          bigint generated always as identity primary key,
  user_id     uuid not null references auth.users(id),
  project_id  bigint not null references projects(id) on delete cascade,
  uid         text not null,             -- triggering application
  rule_id     bigint references ui_alert_rules(id) on delete cascade,
  kind        text not null,
  due_on      date not null,
  message     text,
  read_at     timestamptz,
  created_at  timestamptz not null default now(),
  unique (user_id, rule_id, uid)         -- one alert per rule per application
);
create index on ui_alerts (user_id, read_at, due_on);
```

RLS: strict per-user policies (`using (auth.uid() = user_id)` /
`with check (auth.uid() = user_id)`) on all three; `ui_alerts` is
insert-only by the worker, users get `select` + `update(read_at)` only.
These tables are **UI-owned** (created from the client repo's migrations),
but the worker must tolerate their absence (catch undefined-table, log,
skip) so the worker never blocks the client rollout.

### Generation logic (worker, end of each run, `pam/alerts.py`)
After `projects.group(...)`:
1. If `ui_alerts` doesn't exist → skip (log once).
2. For each starred project touched by this run, for each enabled rule of
   the starring user, evaluate the project's members:
   - `nma_filed` / `nma_approved`: member with `role='nma'` and
     `start_date`/`decided_date` = today → due `today + after_days`.
   - `large_approved`: member `app_size='Large'` (or rule's `app_size`) newly
     decided Permitted/Conditions → due `decided_date + after_days`.
   - `discharge_decided`: member `role='discharge'` newly decided → due
     `decided_date + after_days`.
3. Insert due alerts with the `unique` constraint as idempotency
   (`on conflict do nothing`). Re-runs are safe.

Future-dated rules (e.g. "2 years after approval" = `after_days: 730`) are
inserted at decision time with a future `due_on`; a nightly worker sweep
(`kind='alert_sweep'`, cheap) can also re-scan starred projects for rules
created *after* the triggering event.

---

## 5. PlanIt etiquette compliance (applies to new jobs)

From PlanIt's published guidance: run 18:00–06:00, ≥60 s between requests,
~300 requests/day, identifying User-Agent.

- **User-Agent:** `pam-lead-monitor/1.0 (research; info@simonemorciano.com)`
  — update `USER_AGENT` in `pam/api.py`.
- **Pacing:** new config `api.compliant_delay_seconds: 60` and
  `api.daily_request_cap: 300`, used by `parent_hunt` and sync jobs.
  Backfill jobs keep the existing fast pacing **only until the current
  backfill wave finishes** (owner decision), then flip globally.
- **Night window:** worker gains `fetch_window = (18:00, 06:00)` UTC config;
  `parent_hunt` (and later all fetch jobs) only fetch inside the window —
  jobs claimed outside it are requeued with `not_before` = next 18:00.
- **Request counter:** per-day counter in a tiny `api_usage(day, count)`
  table (or a `jobs.stats` accumulation); when the cap is hit, defer
  remaining fetch jobs to the next window.
- Keep honouring `Retry-After` exactly as now (already implemented).

---

## 6. Touchpoints in existing code

| File | Change |
|---|---|
| `pam/projects.py` | `has_parent_refs`, new root ordering, `root_in_db`, missing-parent upserts |
| `pam/ledger.py` | `last_updated` only on real change; new SCHEMA blocks (or a migrate script) |
| `pam/alerts.py` | new module (alert generation) |
| `pam/main.py` | call alerts after grouping |
| `pam/worker.py` | `parent_hunt` job kind, night window, request cap |
| `pam/api.py` | User-Agent, compliant pacing knob |
| `pam/config.py` | `parent_hunt.active_months`, compliant pacing/window config |
| `scripts/rls.sql` | practices + missing_parents policies |
| `scripts/backfill_projects.py` | rerun to set `has_parent_refs` |
| `scripts/find_missing_parents.py` | new bootstrap |
| `scripts/migrate_2026_10_05.py` | new columns/tables (idempotent) |

## 7. Rollout order

1. Migrate + code: root semantics, `has_parent_refs`, `last_updated` fix.
2. Rerun `backfill_projects.py` (fast, batched), verify `root_in_db`.
3. Bootstrap `missing_parents`; enqueue `parent_hunt` jobs (9-month scope).
4. Flip pacing/User-Agent; keep backfill wave running to completion first.
5. Alerts module ships dark (skips if UI tables absent).
6. Client deploys stars/practices UI; worker starts filling alerts.
