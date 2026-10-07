# Frontend spec — new features (2026-10-05, revised 2026-10-07)

**Repo:** `pam-client` (Next.js + Supabase on Vercel).
**Backend counterpart:** `docs/2026.10.05-backend-spec-new-features.md` in the
`pam` repo — the worker/schema changes land there. This document covers what
the client builds on top. Read `01-database-contract.md` and
`03-project-grouping-ui.md` first.

*2026-10-07 revision: the backend points are now implemented (root semantics,
parent finder, PlanIt etiquette, alert generation). The client has direct
access to the live DB — inspect tables in the Supabase dashboard rather than
relying on DDL copied here, so this doc now lists tables/fields instead of
full SQL.*

---

## 1. Concepts the UI must expose

**Two kinds of metadata — keep them visually distinct:**
- **Global/shared** (same for every user): projects, applications, practices,
  practice links, agent details. Anyone can add practices; everyone sees
  everything.
- **Read-shared, write-own**: stars — everyone sees what others starred
  (keeps outreach coordinated), but each user stars/unstars only their own.
- **Per-user, private**: outreach log, alert rules and alerts. Each user has
  their own and cannot see other users'. Every row is scoped by
  `created_by`/`user_id = auth.uid()`.

## 2. New/changed schema the client uses

The client is connected to the live DB — treat the Supabase dashboard as the
source of truth for column types. Below is what each table is **for** and the
fields the UI touches.

### Worker-owned (read-only for the client; RLS already enforced)

- `projects` — new field **`root_in_db`** (boolean): true when no member
  application cites an unresolved parent reference. Drives the root-presence
  tick (§4). Existing fields the UI uses: `id, name, authority, root_uid,
  latest_uid, n_applications, first_seen, last_updated`.
- `project_applications` — new field **`has_parent_refs`** (boolean);
  existing `role`, `parent_ref`, `is_root` power the tabs in §4.
- `missing_parents` — unresolved parent references the worker is hunting.
  Fields: `authority, reference` (upper-case), `ref_year`, `requested_by`
  (child uid), `project_id`, **`status` = `pending | found | exhausted`**,
  `attempts`, `found_uid`. Powers the "root missing / being fetched" tick.

### Client-created tables (migrations live in the client repo)

**Global/shared** — everyone reads and writes all rows; `created_by =
auth.uid()` is attribution only:
- `practices` — `name` (unique case-insensitive), `website`, `notes`.
  Deleting a practice that still has links is blocked by FK — the UI must
  unlink first.
- `project_practices` — `(project_id → projects, practice_id → practices)`
  pair as PK, plus per-link `notes`.
- *RLS safety net:* the worker repo's `scripts/rls.sql` applies the
  "authenticated read+write, anon denied" policies if the client migrations
  haven't — same result either way.

**Read-shared, write-own:**
- `ui_project_stars` — `(user_id, project_id → projects)` pair as PK.
  Everyone can *read* all stars (coordination), users can only insert/delete
  their own rows (`auth.uid() = user_id`).

**Per-user, private** (`auth.uid() = user_id` on all policies):
- `ui_alert_rules` — `kind`, `after_days` (0 = same day), `app_size`
  (optional filter), `enabled`. Valid kinds: `nma_filed`, `nma_approved`,
  `large_approved`, `discharge_decided`. Full CRUD by the owner.
- `ui_alerts` — `project_id`, `uid` (triggering application), `rule_id`,
  `kind`, `due_on`, `message`, `read_at`. Unique on `(user_id, rule_id,
  uid)`. **Select + update of `read_at` only — inserts are worker-only** (no
  client insert policy).
- `ui_outreach` — private outreach log (own rows only); schema defined with
  the outreach feature, not this spec.

Enable RLS on all five client tables in the same migration that creates
them; the policy rules above are the whole model.

## 3. Leads/projects list — project-centric search

Queries run against **projects**, not applications:

- **Filed between X and Y** → `projects.first_seen` (earliest member filing,
  includes the root when present) in [X, Y]. Note: `first_seen` is when *we*
  first saw it; for display-quality "filed date" use the root member's
  `start_date` — the backend may add `projects.filed_date`; until then join
  `applications` on `root_uid` and take its `start_date` (null-safe:
  fall back to `first_seen`).
- **Updated between W and Z** → `projects.last_updated` in [W, Z] — bumped
  when any member application is filed or changes.

Supabase JS sketch:
```ts
let q = supabase.from("projects").select(
  "id, name, authority, n_applications, root_in_db, last_updated, root_uid, latest_uid");
if (filedFrom) q = q.gte("applications!projects_root_uid_fkey", ...)  // see note
```
There is no FK on `root_uid` (soft link), so the practical v1 pattern is two
queries or a small RPC/view. **Recommended: create a `projects_feed` view**
in the client migrations that joins `applications root ON root.uid =
projects.root_uid` and exposes `filed_date` — then filtering is trivial:
```sql
create view projects_feed with (security_invoker = true) as
select p.*, root.start_date as filed_date, root.decided_date as root_decided_date
from projects p
left join applications root on root.uid = p.root_uid;
```

## 4. Project detail page — application tabs

Tabs in **filed order** (`start_date` ascending), root first
(`is_root` / `uid = root_uid`). Each tab/card shows:
- **Role label** from `project_applications.role`, humanised:
  `original` → "Root application", `discharge` → "Discharge of conditions",
  `variation` → "Variation (s73)", `nma` → "Non-material amendment".
- **Filed date** (`start_date`).
- **Status** with colour coding by `app_state`:
  Undecided = amber/"pending"; Permitted/Conditions = green + decision label
  + `decided_date`; Rejected/Withdrawn = red/grey.
- **Last update date** (`applications.last_updated`) if it differs from
  filing.
- Root tab additionally: **presence tick** — `✓ in database` when
  `projects.root_in_db` is true. Otherwise look up the project's unresolved
  refs in `missing_parents`: `pending` → "original application predates our
  records (being fetched)"; `exhausted` → "not found on PlanIt" (a `found`
  ref means regrouping is imminent — treat like `pending`).

Suggested colour tokens: pending amber-500, approved emerald-600,
rejected rose-600, withdrawn/neutral zinc-500.

## 5. Practices UI

- **Practice register page:** searchable list of `practices` (name, website,
  notes), with the projects each is linked to.
- **Add practice:** from the register, or inline from a project page
  ("Link practice" → autocomplete existing by `name` or create new).
  Autocomplete query: `ilike` on `lower(name)` (the unique index dedupes
  case-insensitively; handle the 23505 conflict by selecting the existing
  row).
- **Suggestion chip (nice-to-have):** if `applications.agent_company` exists
  on project members but no practice is linked, offer "Link {agent_company}?"
- Per-link `notes` editable inline on the project page.

## 6. Stars & alerts

- **Star toggle** lives on the **project**, not the application: starring any
  application stars its project (`ui_project_stars`). Stars are
  **read-shared**: show your own star state prominently, and indicate when a
  project is starred by another user (e.g. "starred by X" — join
  `auth.users` email via a view or edge function; do not expose user ids
  raw). This coordination is deliberate: it prevents two users targeting the
  same practice unknowingly. Write/delete is own-rows only.
- **Alerts inbox:** in-app list of `ui_alerts` for the user, unread first,
  grouped by `due_on`; "mark read" sets `read_at`. No email in v1.
- **How alerts are generated (worker, already live — read-only for you):**
  at the end of each daily sync run (overnight, 18:00–06:00 UK time), for
  every **starred project touched by that run**, the worker evaluates the
  project's members against the starring user's enabled rules and inserts
  due alerts. Key semantics to set user expectations:
  - A rule fires when the triggering event date falls within the **last 3
    days** (tolerance for PlanIt publishing filings/decisions late).
  - `due_on` = event date + `after_days` (so `after_days: 730` on an
    approval creates a far-future-due alert immediately — group by `due_on`
    keeps these out of the way).
  - At most **one alert per (user, rule, application)**, ever — no
    duplicates on re-runs.
  - Alerts only cover **starred** projects, and only rules that existed
    *before* the event (a later "sweep" for retroactive rules is a planned
    backend addition — worth an empty-state hint: "rules apply from when
    you create them").
  - Until the client ships these tables, the worker skips generation
    silently — deploy order is flexible.
- **Alert settings page:** CRUD for the user's `ui_alert_rules`. Preset
  templates to offer:
  - "Non-material amendment filed" (kind `nma_filed`, 0 days)
  - "Non-material amendment approved" (`nma_approved`, 0 days)
  - "Large application approved → follow up in 2 years" (`large_approved`,
    `after_days: 730`, `app_size: 'Large'`)
  - "Discharge of conditions decided → reach out in 3 months"
    (`discharge_decided`, `after_days: 90`)
- Empty state explains alerts only cover **starred** projects.

## 7. What the client must NOT do

- No writes to `projects`, `project_applications`, `missing_parents`,
  `applications` — worker-owned (RLS enforces).
- No alert generation client-side — the worker computes them; the client only
  reads/marks read. (Worker tolerates missing tables, so deploy order is
  flexible.)
- Don't infer "root present?" from dates — use `projects.root_in_db`.

## 8. Deliverables checklist

1. Migrations: practices/project_practices + ui_* tables + RLS +
   `projects_feed` view.
2. Leads list → project-grouped search with dual date-range filters.
3. Project page: tabs, colour-coded states, root presence indicator.
4. Practice register + linking.
5. Project-level starring.
6. Alerts inbox + settings with the preset rules.
