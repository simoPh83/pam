# Database contract (Supabase Postgres) — as of 2026-10-04

Live data: ~138k rows in `applications`. Authorities: Westminster, Barnet, Enfield,
Camden, Greenwich, Southwark, Lambeth, Lewisham, Brent, Islington, Newham, Tower
Hamlets, Hackney, Sutton, City (of London) and more being backfilled (33 London
boroughs total). Data source: PlanIt (planit.org.uk) — field names mirror it.

**Types:** date columns are real `date`; `last_changed`/`last_different`/`last_scraped`,
`state_history.observed_at` are `timestamptz`; `first_seen`/`last_updated` are `date`; `in_leads_sheet` is `boolean`; `lat`/`lng`/`distance_km` are `double precision`;
`other_fields_json` is `jsonb`.

## `applications` (one row per planning application; PK `uid`)
Core: `uid`, `reference`, `authority`, `app_state`, `app_size`, `start_date`,
`decided_date`, `first_seen`, `last_updated`, `in_leads_sheet` (boolean).

- `app_state` values: Permitted (~70k), Undecided (~33k), Rejected, Conditions
  (approved with conditions), Withdrawn, Unresolved, NULL.
- `app_size`: Small (most), Medium, Large, NULL. Medium/Large are the interesting ones
  for architecture photography.
- `in_leads_sheet = true` means the fetcher flagged it as a lead: state is Permitted or
  Conditions and the decision was recent when first seen (older historical approvals
  from backfills are stored but flagged false). Latch: once 1 it stays true. Don't rely on it
  as the only lead definition — build a `leads` view with explicit criteria
  (state in Permitted/Conditions, app_type not "Trees", decided within N months,
  size filter) and let the UI filter further.

Descriptive: `address`, `postcode`, `description`, `app_type`, `development_type`,
`ward`, `parish`, `uprn`, `lat`, `lng`, `distance_km`, `n_documents`, `n_comments`,
`n_constraints`, `n_dwellings`, `n_statutory_days`.
`application_type` is the council's own wording (~540 distinct values, e.g. "Householder", "Full Planning Permission"): use it for filtering/keyword search; `app_type` is only 8 coarse buckets. `applicant_address` (text) is also available.

Agent / applicant (the architect's details are the outreach target):
- `agent_name` — a **person's** name only.
- `agent_company` — practice/company name (legacy values were migrated here).
- `agent_address` — agent's address.
- `agent_display` — company if known, else person name. **Use this for display and
  grouping** (later: a normalised company key for practice dedup).
- `applicant_name`.

Dates (`date`; the last three are `timestamptz`): `date_received`, `date_validated`, `target_decision_date`,
`consultation_end_date`, `consultation_start_date`, `neighbour_consultation_start_date`, `neighbour_consultation_end_date`, `comment_date`, `decision_published_date`, `application_expires_date`, `decision_issued_date`,
`permission_expires`, `appeal_date`, `appeal_decision_date`, `decided_date`,
`last_changed`, `last_different`, `last_scraped`.

Decision/appeal: `decision`, `decided_by`, `source_status`, `appeal_reference`,
`appeal_result`, `planning_portal_id`.

URLs: `council_url`, `docs_url`, `comment_url`, `map_url` are stored **shortened**
(tail only). A value starting with `http` is already absolute (different host).
**Always read full URLs from the view `applications_full`**
(`uid, council_url, docs_url, comment_url, map_url, source_url`) — join on `uid`.

`other_fields_json` (TEXT JSON): leftover PlanIt fields; rarely needed. Parse
defensively. Don't select it in list queries.

## `state_history`
`id, uid, observed_at, old_state, new_state, decided_date, decision, last_different`.
One row per observed state change (e.g. Undecided → Permitted). Index on `uid`.
Useful for "recently approved" and for timelines on the detail page.
Note (2026-10-06): first-sighting rows are no longer recorded (they duplicated
`applications.first_seen` and were purged — 292k rows, ~54 MB). Start timelines
from `applications.first_seen`, then apply these change rows.

## `authority_urls`
`(authority, field, base_url)` — used by `applications_full`; UI normally doesn't
need it.

## `jobs` (fetcher job queue — read-only, good for an admin/status page)
`id, kind (backfill|sync), area, start_date, end_date, since, priority, status
(pending|running|done|failed), attempts, not_before, created_at, started_at,
finished_at, stats (jsonb), error, dedupe_key`.
Nice-to-have: a "Data freshness" page showing last done sync per area.

## `fetch_progress`
Fetcher-internal resume bookkeeping. Ignore.

## `projects` / `project_applications` (site grouping, added 2026-10-04)
Applications on the same development site are grouped into one project so
outreach is per site, not per application. Clustering key:
`(authority, address_key)` where `address_key` is the normalised address.

`projects`: `id, authority, name (shortest address in cluster), address_key,
root_uid (earliest original application), latest_uid, n_applications,
first_seen, last_updated, created_at`.

`project_applications`: `(project_id, uid)` membership + `parent_ref` (best
parent reference extracted from the description — the one resolving to the
earliest application we hold), `is_root`, `role`
(`original|variation|discharge|nma`).

Maintained by the worker at the end of every run; updated incrementally.

## The `leads` view (read this, not the raw table)
All `applications` where state is Permitted/Conditions and type isn't Trees,
plus `project_id` and `n_applications` (joined from projects). Use
`project_id` to collapse the list to one row per site (show the `latest_uid`
row, badge with `n_applications`).

## What the UI must add (UI-owned tables; suggested)
```sql
-- outreach log, one row per contact attempt
create table ui_outreach (
  id uuid primary key default gen_random_uuid(),
  uid text not null,                 -- applications.uid (soft link)
  contacted_at timestamptz not null default now(),
  channel text,                      -- email | phone | linkedin | other
  to_name text, to_email text,
  status text not null default 'contacted', -- contacted|replied|follow_up|won|lost|no_interest
  follow_up_on date,
  notes text,
  created_by uuid not null default auth.uid() references auth.users(id),  -- owner; outreach is private per user (RLS: auth.uid() = created_by)
  created_at timestamptz not null default now()
);
create index on ui_outreach (uid);
create index on ui_outreach (status, follow_up_on);

-- per-lead state (star, hide, notes)
create table ui_lead_meta (
  uid text primary key,
  starred boolean default false,
  hidden boolean default false,
  notes text,
  updated_at timestamptz not null default now()
);
```
RLS for UI tables: `enable row level security` + one policy `for all to authenticated using (true) with check (true)` (shared). Show who logged each entry (join auth user email via a view or store display name).

Later (Phase 4): `ui_practices(name, website, email, notes)`, `ui_contacts`, and a
mapping from `agent_display` (normalised) → practice.

## Suggested indexes (verify with EXPLAIN; add via migration)
`applications(app_state, decided_date desc)`, `applications(authority)`,
`applications(app_size)`, `applications(agent_display)`, `applications(postcode)`,
and a trigram/FTS index on `description`/`address` if keyword search is slow.

## Suggested `leads` view (adjust with the user)
```sql
create or replace view leads as
select a.uid, a.reference, a.authority, a.address, a.postcode, a.description,
       a.app_type, a.app_size, a.app_state, a.decided_date, a.agent_display,
       a.agent_name, a.agent_company, a.agent_address, a.applicant_name,
       a.lat, a.lng, a.n_dwellings, a.first_seen, a.last_updated
from applications a
where a.app_state in ('Permitted','Conditions')
  and coalesce(a.app_type,'') <> 'Trees';
```
Create it with `security_invoker = true` so RLS on the base table applies.

## Connection details (ask the user; never commit)
- Supabase project URL + anon key → `NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
- Service-role key only if strictly needed server-side → `SUPABASE_SERVICE_ROLE_KEY`.
- The worker uses the Session pooler; the UI should use supabase-js or the
  Transaction pooler (6543).
