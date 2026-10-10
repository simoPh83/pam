# Scheme-level grouping — reference-connected families

**Date:** 2026-10-10 · **Repo:** `simoPh83/pam` (worker + schema)
**Supersedes:** the address-cluster grouping model of
`docs/2026.10.04 project-grouping-spec.md` (kept for history).
**UI spec:** deferred — a companion spec for `pam-client` will be written once
the migration is running (see `grouping_review` below).

## Why

Address clustering (`authority + normalised address`) groups *sites*, not
*schemes*. Measured on 2026-10-10 (~180.5k applications, ~84.7k projects):

- ~26,970 address-cluster projects contain 2+ applications classified
  `original`; 21,317 of them have **no other members at all** — unrelated
  applications glued together purely by address.
- `role='original'` is our own heuristic fallback ("doesn't look like a
  discharge/variation/NMA"), **not** a PlanIt field — it cannot be the
  definition of a project root.
- Reference links are strong evidence: parsing parent refs from descriptions
  finds 43,506 mentions, 26,189 of which resolve to stored applications
  (same authority, exact or suffix-stripped match).

## Model

**A project is one scheme: a connected component of applications linked by
parent references**, resolved authority-wide (not just within an address
cluster — 629 components cross address-cluster boundaries).

- Edges: application A cites ref R in its description, R resolves to stored
  application B (same authority) → A→B. Components are undirected connected
  components of that graph (a chain discharge→variation→original is one
  scheme even though the discharge doesn't cite the original directly).
- **Root**: a component member with no *resolved* parent ref. `root_uid` =
  earliest root (start_date, uid). "Original" and "root" become the same
  thing by construction; `project_applications.role` stays as display
  metadata only.
- **Address is no longer a join key.** It is used (a) to auto-attach
  reference-less follow-ups when unambiguous, (b) to offer candidates in the
  review queue, (c) for site-level display in the UI (derived, later).

### `projects.grouping_state`

| Value | Meaning | UI |
|---|---|---|
| `clean` | exactly one root, role original, no unresolved refs | normal |
| `ambiguous` | component has 2+ roots (kept merged provisionally) | badge + "resolve" prompt |
| `provisional` | root is non-original, or root has unresolved refs (parent missing) | subtle "incomplete chain" indicator |

The flag is written by the migration script for existing rows and maintained
by the worker's incremental grouping for new/changed rows, so it never drifts.

### `project_applications.linked_by`

Provenance of each membership: `reference` (parsed ref edge), `inferred`
(address heuristic, see below), `manual` (set by a user resolution).

## Auto-attach heuristic for reference-less follow-ups

A non-original application with **no parsed refs at all** (e.g. "Approval of
details…" with no reference quoted) is auto-attached to the scheme at its
address **iff that address has exactly one scheme** (`linked_by='inferred'`).
Otherwise it becomes its own provisional project and goes to the review queue.

Measured queue sizes (2026-10-10):

| Reason | Items | Question |
|---|---:|---|
| `multi_root` | 787 components (2,943 apps) | one scheme or split? |
| `unlinked_multi_scheme` | 6,457 apps | which scheme at this address? |
| `unlinked_no_scheme` | 4,630 apps | standalone scheme or missing parent? |
| auto-attached (no review) | 3,376 apps | — |
| unresolved-ref components | 14,914 (16,085 apps) | owned by `missing_parents` / parent-hunt, not the queue |

≈ 11,874 review items → the UI needs bulk actions and per-row suggestions.

## `grouping_review` table

```sql
create table grouping_review (
  id          bigint generated always as identity primary key,
  authority   text not null,
  project_id  bigint not null references projects(id) on delete cascade,
  uid         text,                    -- the ambiguous application (null for multi_root)
  reason      text not null,           -- multi_root | unlinked_multi_scheme | unlinked_no_scheme
  candidates  jsonb not null,          -- [{uid, description, app_type, start_date, project_id}]
  status      text not null default 'pending',  -- pending | resolved | dismissed
  resolution  text,                    -- chosen project id as text, 'new', or 'split'
  resolved_by uuid,
  resolved_at timestamptz,
  created_at  timestamptz not null default now()
);
```

RLS: worker inserts; authenticated users `select` and column-restricted
`update (status, resolution, resolved_by, resolved_at)`. A resolution
applies a worker-side regroup of the affected uids with `linked_by='manual'`
and clears the row.

## Migration (`scripts/regroup_schemes.py`)

Dry-run by default (single transaction, rolled back); `--apply` commits in
phases (DDL → projects+repoint → memberships+review → aggregates) so WAL can
recycle between checkpoints. Worker must be stopped for `--apply`.

**Applied 2026-10-10.** Results: 154,535 scheme projects (was 84,703
address-cluster projects); 125,755 clean / 28,535 provisional / 245
ambiguous; 179,129 `reference` + 1,489 `inferred` memberships; 12,969 pending
`grouping_review` rows; 64 old project ids retired and re-pointed, 0
orphaned. Verified: every application is a member of exactly one project and
every project has exactly one `is_root`.

1. DDL: drop `projects(authority, address_key)` unique; `address_key` nullable;
   add `projects.grouping_state`, `project_applications.linked_by`; create
   `grouping_review` + RLS.
2. Load all applications; parse refs; resolve authority-wide; build components
   (DSU).
3. Classify components; run the address auto-attach heuristic; build review
   rows.
4. **Preserve old project ids**: a component inherits the id of the old
   project whose `root_uid` it contains (unsplit clusters keep their id, so
   most `ui_*` references stay valid). Old projects whose id is not inherited
   are *retired*; their dependents are re-pointed to the successor (the
   component containing their old `root_uid`):
   `ui_project_stars`, `ui_project_meta`, `ui_project_practices`,
   `ui_project_agent_presence`, `ui_project_root_tsv` (dedupe on conflict),
   `missing_parents`, `project_aliases`.
5. Rebuild `project_applications` (`TRUNCATE` + bulk insert with `linked_by`,
   `is_root`), upsert projects, insert `grouping_review` rows.
6. Recompute aggregates (`n_applications`, `latest_uid`, `name`,
   `first_seen`, `last_updated`, `root_in_db`) set-based; `root_uid` and
   `grouping_state` are **not** recomputed by this step.
7. Print validation counts; compare against the table above.

## Worker changes (done 2026-10-10, before restart)

Implemented in `pam/projects.py` and validated read-only + synthetic-tx:

- `group()` — an application whose parent ref resolves to a member of an
  existing project joins that project directly (references trump the address
  bucket); otherwise it lands in the address project as a provisional seed.
  Aliases still steer repeats. `merge_linked()` removed (superseded).
- `refresh()` — aggregates only; no longer picks `root_uid` by date tiebreak.
- `_set_root_and_state()` — root = member with no *resolved* parent ref
  (earliest on tie; deterministic fallback for reference cycles). Sets
  `grouping_state`: one original root & no unresolved refs → `clean`, one
  non-original/unresolved root → `provisional`, zero or multiple roots →
  `ambiguous`.
- Validated: migration roots reproduced on 300/300 clean and 198/200
  ambiguous projects (the 2 are reference cycles with interchangeable roots);
  synthetic sync test confirmed a citing discharge joins its cited scheme
  and an unrelated new original at the same address becomes its own project.
- `grouping_review` insertion on new ambiguity is **not yet wired** into the
  worker (the table + migration backfill exist; the UI resolution endpoint
  will call a regroup helper that also clears rows).
- The `main_uid` headline spec (`pam-client` construction-timing docs) is
  rebased onto scheme-level projects afterwards.

## Out of scope

- Splitting multi-scheme *components* automatically (e.g. Barnet Gas Works:
  references genuinely tie three schemes together) — that's what the review
  queue is for.
- Site-level browsing UI (derived from shared `address_key`) — pam-client spec.
