# Project grouping — spec for the Python worker (Railway)

**Date:** 2026-10-04
**For:** `simoPh83/pam` (fetcher repo) — the worker owns this. The web client
(`pam-client`) only reads the result.

## Why

One physical development produces many planning applications over the years
(original permission → s73 variations → condition discharges → NMAs). Outreach
must be per **project**, not per application, or we email the same practice
multiple times about the same scheme.

Measured on 2026-10-04 (~162k rows):

- 39,651 rows are discharge/amendment-type applications; ~94% contain an
  extractable parent reference in `description`.
- Parent refs only resolve within `applications` ~13% of the time (parents
  predate the backfill window) — so parent-linking alone is insufficient.
- Address clustering works well: `upper(regexp_replace(address, '\s+', ' '))`
  + `authority` groups same-site applications reliably (e.g. TwelveTrees Park:
  78 rows; Biscuit Factory Southwark: 178 rows).
- UPRN coverage is only ~4% — not usable as the primary key. Keep it as a
  tiebreaker where present.

## Proposed schema (two small tables, ids + short text only)

```sql
-- One row per distinct development site/project.
create table projects (
  id            bigint generated always as identity primary key,
  authority     text not null,
  name          text,                    -- best short address line (see naming below)
  address_key   text not null,           -- normalised address used for clustering
  root_uid      text,                    -- uid of earliest non-discharge app, if known
  latest_uid    text,                    -- uid of most recently decided/updated app
  n_applications int not null default 1,
  first_seen    date,
  last_updated  date,
  created_at    timestamptz not null default now(),
  unique (authority, address_key)
);

-- Membership: which applications belong to the project.
create table project_applications (
  project_id   bigint not null references projects(id) on delete cascade,
  uid          text not null,            -- applications.uid (soft link, no FK)
  parent_ref   text,                     -- extracted parent reference (raw, for debugging)
  is_root      boolean not null default false,
  role         text,                     -- 'original'|'variation'|'discharge'|'nma'|'other'
  primary key (project_id, uid)
);
create index on project_applications (uid);
```

Size estimate: ~162k membership rows × ~50 bytes + ~40–60k project rows
× ~150 bytes ≈ **15–20 MB total** — comfortable on the 500 MB free tier.
No descriptions, no JSON, no duplicated fields: the UI joins back to
`applications` for display.

## Normalisation rules

`address_key` (per authority):

1. Uppercase, trim, collapse all whitespace to single spaces.
2. Strip punctuation except `/` and `-` inside tokens.
3. Remove trailing postcode (already have `postcode` column separately).
4. Normalise common tokens: `FLAT|UNIT|APARTMENT` variants → keep as-is (they
   distinguish sub-units; only cluster at building/site level — drop unit
   numbers: leading `FLAT \d+|UNIT \d+|ROOM \d+` removed).
5. AKA handling: keep the full string ("X AKA Y") — variants like `X` vs
   `X AKA Y` will not cluster; acceptable for v1, revisit with fuzzy matching
   (pg_trgm) only if it proves to matter.

`role` classification (from description keywords, case-insensitive):

- `discharge`: contains `pursuant to` / `approval of details` / `condition`
- `variation`: contains `variation of condition` / `s73` / `section 73`
- `nma`: contains `non-material amendment` / `s96a` / `section 96a`
- `other`/`original`: everything else (FUL, OUT, HHD, etc.)

`parent_ref` extraction (stricter than the 2026-10-04 probe):

```python
PARENT_RE = re.compile(
    r"(?:permission|application|consent)\s*(?:ref(?:erence)?[.:]?\s*|no[.:]?\s*|\(\s*)?"
    r"([A-Z0-9][A-Z0-9/_.-]{4,25}\d[A-Z0-9/_.-]*)",
    re.I,
)
```

- Must contain at least one digit and be ≥ 6 chars (kills `roval`, `ragraphs`).
- Strip trailing punctuation; upper-case.
- Optionally validate per-authority shape (e.g. Camden `\d{4}/\d{4}/P`,
  Tower Hamlets `PA/\d{2}/\d{5}(/[A-Z0-9]+)?`) — a format mismatch table is
  fine to keep loose in v1; log unmatched for tuning.

## When to run

**Incremental, at the end of every sync run** (not during fetch):

1. For each row inserted/updated by the run:
   - compute `address_key` + `role` + `parent_ref`;
   - find `projects` row by `(authority, address_key)`; create if missing;
   - upsert into `project_applications`;
   - update the project's `latest_uid`, `n_applications`, `last_updated`.
2. Set `root_uid`/`is_root`: earliest `role='original'` (FUL/OUT) in the
   cluster, else earliest member.
3. If `parent_ref` resolves to a member of a **different** project, log it
   (merge candidates — rare; handle manually at first).

A **one-off backfill** job processes all existing rows in batches by authority
(the whole table is ~160k rows; a few minutes of sequential work).

## RLS

Same model as the other fetcher tables (worker writes, users read):

```sql
alter table projects enable row level security;
alter table project_applications enable row level security;
revoke all on projects from anon;
revoke all on project_applications from anon;
grant select on projects to authenticated;
grant select on project_applications to authenticated;
create policy "authenticated read" on projects for select to authenticated using (true);
create policy "authenticated read" on project_applications for select to authenticated using (true);
```

## What the UI will do with it

- Leads table: collapse rows by project (show `latest_uid` row, badge with
  `n_applications`), or keep flat list with a "project" grouping toggle.
- Lead detail: "other applications on this site" via `project_applications`.
- Later: practices link to projects; completion-window scoring per project
  (latest discharge date − root permission date).

## Out of scope (for now)

- Fuzzy/cross-authority address matching (pg_trgm) — evaluate after v1.
- Architect identification — remains an on-demand enrichment step on the
  outreach shortlist (see research/2026-10-04-savills-architects.md).
- Householder sub-units: grouping at site level only.
