# Frontend spec — new features (2026-10-05)

**Repo:** `pam-client` (Next.js + Supabase on Vercel).
**Backend counterpart:** `docs/2026.10.05-backend-spec-new-features.md` in the
`pam` repo — the worker/schema changes land there. This document covers what
the client builds on top. Read `01-database-contract.md` and
`03-project-grouping-ui.md` first.

---

## 1. Concepts the UI must expose

**Two kinds of metadata — keep them visually distinct:**
- **Global/shared** (same for every user): projects, applications, practices,
  practice links, outreach log. Anyone can add; everyone sees everything.
- **Per-user**: stars and alert rules/alerts. Each user has their own.

## 2. New/changed schema the client uses

Worker-owned (read-only for the client):

```text
projects      + root_in_db boolean   -- true when no member cites an
                                     -- unresolved parent reference
project_applications + has_parent_refs boolean
missing_parents (authority, reference, ref_year, status, found_uid ...)
                -- read-only; powers the "root missing" tick
```

Client-created tables (write migrations in the client repo; RLS as below):

```sql
-- GLOBAL: architectural practices (reusable entity → many projects)
create table practices (
  id bigint generated always as identity primary key,
  name text not null,
  website text,
  notes text,
  created_by uuid references auth.users(id),
  created_at timestamptz not null default now()
);
create unique index on practices (lower(name));

create table project_practices (
  project_id bigint not null references projects(id) on delete cascade,
  practice_id bigint not null references practices(id) on delete cascade,
  notes text,
  created_by uuid references auth.users(id),
  created_at timestamptz not null default now(),
  primary key (project_id, practice_id)
);
-- RLS: authenticated read+write (all), anon denied. Shared by design.

-- PER-USER: stars at project level
create table ui_project_stars (
  user_id uuid not null references auth.users(id),
  project_id bigint not null references projects(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, project_id)
);

-- PER-USER: alert rules
create table ui_alert_rules (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id),
  kind text not null,        -- 'nma_filed'|'nma_approved'|'large_approved'|'discharge_decided'
  after_days int not null default 0,
  app_size text,             -- optional filter ('Large' etc.)
  enabled boolean not null default true
);

-- PER-USER: generated alerts (worker inserts; user reads + marks read)
create table ui_alerts (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id),
  project_id bigint not null references projects(id) on delete cascade,
  uid text not null,
  rule_id bigint references ui_alert_rules(id) on delete cascade,
  kind text not null,
  due_on date not null,
  message text,
  read_at timestamptz,
  created_at timestamptz not null default now(),
  unique (user_id, rule_id, uid)
);
```

RLS for the per-user tables — strict ownership:
```sql
alter table ui_project_stars enable row level security;
alter table ui_alert_rules  enable row level security;
alter table ui_alerts       enable row level security;
create policy "own stars" on ui_project_stars for all to authenticated
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own rules" on ui_alert_rules for all to authenticated
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "read own alerts" on ui_alerts for select to authenticated
  using (auth.uid() = user_id);
create policy "mark read" on ui_alerts for update to authenticated
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
-- no insert for authenticated: the worker generates alerts
```

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
  `projects.root_in_db`, otherwise "original application predates our records
  (being fetched)" — driven by `missing_parents.status`
  (`pending`/`fetching`/`exhausted`).

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
  application stars its project (`ui_project_stars`). Show star state on the
  leads list (via join on `project_id`) and project page.
- **Alerts inbox:** in-app list of `ui_alerts` for the user, unread first,
  grouped by `due_on`; "mark read" sets `read_at`. No email in v1.
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
