# Backend spec: multi-root review maintenance + user-triggered split

**Date:** 2026-10-12 · **For:** `simoPh83/pam` (worker + schema)
**Frontend:** `simoPh83/pam-client` · **Depends on:**
`2026-10-10-scheme-grouping-spec.md`, `2026-10-10-grouping-review-ui-spec.md`
(those docs live in the `pam` repo; this spec is kept in `pam-client/docs`
alongside the UI that consumes it).

## Problem

Two gaps leave `ambiguous` projects unactionable in the UI:

1. **Review rows are not maintained by the worker.** The 2026-10-10
   migration backfilled `grouping_review` once, but `_set_root_and_state()`
   in `pam/projects.py` still does not insert/refresh `multi_root` rows when
   incremental grouping marks a project `ambiguous`. Measured 2026-10-12:
   **940 projects are `grouping_state='ambiguous'` but only 208 have a
   pending `multi_root` review** — the other 737 render as *"This address
   contains multiple candidate schemes. Review details are not currently
   available."* (There are also 0 non-pending `multi_root` rows, so review
   rows evidently get lost — e.g. via `on delete cascade` — when a project
   is regrouped, with no replacement.)
2. **Split is not implemented anywhere.** `apply_grouping_resolution`
   explicitly raises `'multi_root' split is performed by the worker, not
   the RPC`, and the worker has no split/regroup helper. Users cannot turn
   an address holding two parent-less main applications into two projects.

An ambiguous project = a reference-connected component with **2+ candidate
roots** (members with `has_parent_refs = false`, i.e. no resolved parent
ref — typically full/original applications that are each the plausible
"main" application of a distinct scheme that happen to share an address or
a stray cross-reference). The user reviews the candidates and either keeps
them merged (already supported) or asks for a **split**: each candidate
root, plus the members reachable from it through resolved reference edges,
becomes its own project.

## Design decision: queued, not synchronous

Split execution must reuse the worker's reference parsing + component
logic (Python), not a reimplementation in plpgsql that would drift. So the
RPC only **records the decision and enqueues a high-priority job**; the
worker performs the split. The worker polls every 30 s and claims by
`priority DESC`, so a split starts within about a minute when idle, and
jumps ahead of queued backfills/syncs.

## Part 1 — Worker maintains `multi_root` review rows

In `pam/projects.py`, wherever `grouping_state` is (re)derived
(`_set_root_and_state()`, and the regroup/parent-hunt paths that call it):

- **On becoming or remaining `ambiguous`:** upsert one pending
  `grouping_review` row `(reason='multi_root', project_id)` — insert if no
  pending row exists, otherwise refresh `candidates` in place (the snapshot
  goes stale as memberships change). Candidates = one entry per member with
  `has_parent_refs = false`: `{uid, description, app_type, start_date}`
  (join `applications`; matches the shape the migration wrote).
- **On leaving `ambiguous`** (regroup, parent-hunt resolution, split): mark
  any pending `multi_root` rows for the project `status='resolved'`,
  `resolution='obsolete'` (no user action happened; `resolved_by` stays
  NULL). Same treatment when a project stops being `provisional`/`clean`
  eligibility changes — the rule is simply: a pending `multi_root` row
  exists iff the project is currently ambiguous.
- **Backfill (one-off):** insert rows for the existing 737 ambiguous
  projects lacking one. A small script in the style of
  `scripts/orphans_to_review.py`, or a `--fix-reviews` pass in
  `scripts/regroup_schemes.py`; dry-run by default.

## Part 2 — `split_requests` table

```sql
create table split_requests (
  project_id   bigint primary key references projects(id) on delete cascade,
  requested_by uuid not null default auth.uid(),
  status       text not null default 'pending',  -- pending | running | done | failed
  error        text,
  created_at   timestamptz not null default now(),
  finished_at  timestamptz
);

alter table split_requests enable row level security;
grant select on split_requests to authenticated;
create policy "authenticated read" on split_requests
  for select to authenticated using (true);
-- inserts/updates happen only via SECURITY DEFINER RPC and the worker
```

One row per project (PK) makes repeat clicks idempotent for free. `select`
for authenticated users lets the UI show a "split in progress" state.

## Part 3 — RPC: accept `'split'` for `multi_root`

Extend `apply_grouping_resolution` (definition maintained in
`scripts/apply_grouping_rpc.py`), replacing the current
`RAISE EXCEPTION 'multi_root split is performed by the worker, not the RPC'`
branch:

```text
IF p_resolution = 'split' THEN
  -- idempotent: one pending request per project
  INSERT INTO split_requests (project_id, requested_by)
  VALUES (v_pid, auth.uid())
  ON CONFLICT (project_id) DO UPDATE
    SET status = 'pending', error = NULL, requested_by = auth.uid(),
        created_at = now()
    WHERE split_requests.status IN ('done', 'failed');
  -- high-priority job; dedupe_key prevents duplicate queued jobs
  INSERT INTO jobs (kind, area, priority, dedupe_key)
  VALUES ('split', r.authority, 100, 'split:' || v_pid)
  ON CONFLICT (dedupe_key) DO NOTHING;
  RETURN jsonb_build_object('action', 'split_queued', 'project_id', v_pid);
END IF;
```

Notes:

- The review row **stays pending** until the worker completes the split —
  it is the worker that marks it `resolved` with `resolution='split'`.
  (If the RPC marked it resolved immediately, a failed split would silently
  lose the review.)
- Validate the project is still ambiguous before queueing (a stale review
  after a parent-hunt regroup should return a clear error, not queue junk).
- Priority 100 > backfill's optional 10 > default 0; the `jobs.kind` column
  is unconstrained text, so `'split'` needs no DDL beyond updating the
  comment in `JOBS_DDL`.

## Part 4 — Worker: `kind='split'` job handler

In `pam/worker.py::process()`, branch on `job["kind"] == "split"` **instead
of** `execute(...)` — a split is pure local DB work (no PlanIt calls, no
`prune`). Parse the project id from `dedupe_key`, then call a new
`projects.split_project(conn, project_id) -> dict` whose stats go into the
job row. `drain_regroup_pending()` still runs afterwards as today.

### `projects.split_project` algorithm

Reuse the parsing/DSU machinery from `scripts/regroup_schemes.py` (extract
the shared bits into `pam/projects.py` or a shared module rather than
copying):

1. **Load** the project's members (`project_applications` joined to
   `applications`), re-parse parent refs from descriptions, and resolve
   them authority-wide (same exact + suffix-stripped matching as
   `group()`), but **keep only edges whose both ends are members of this
   project**. Resolution here is table-only — no API.
2. **Components**: undirected connected components (DSU) over those edges.
3. **Id retention** (same rule as the migration): the component containing
   the project's current `root_uid` keeps the existing `projects.id`.
   Every other component gets a fresh `projects` row (authority copied;
   `address_key`, `name`, `first_seen`, `last_updated` recomputed by
   `refresh()`; `grouping_state` re-derived per below).
4. **Move memberships**: update `project_applications.project_id` for
   non-retained components. `linked_by` is untouched — edges are still
   `reference`/`inferred`; the user's *decision* is recorded on the review
   row, not on memberships.
5. **Re-point references to moved components** (mirroring migration step
   4): `missing_parents.project_id` and `project_aliases.project_id`
   follow the component containing their `requested_by` uid / matching
   alias. **UI-owned rows (`ui_project_stars`, `ui_project_meta`,
   `ui_project_practices`, `ui_project_agent_presence`,
   `ui_project_root_tsv`) stay on the retained project id** — we can't know
   which scheme the user cared about; the retained component (old root) is
   the least surprising default. Documented trade-off, revisit if users
   complain.
6. **Re-derive per component**: `refresh()` aggregates, then
   `_set_root_and_state()` (root = member with no resolved parent ref,
   earliest on tie; deterministic fallback for reference cycles). A
   component with 2+ roots of its own lands back in `ambiguous` and gets a
   fresh `multi_root` review row via Part 1 — this is correct, not a
   failure (e.g. a stray reference cycle).
7. **Close out**: mark the project's pending `multi_root` review rows
   `status='resolved', resolution='split', resolved_by=<requester>`;
   delete the `split_requests` row (or set `status='done'` — prefer delete,
   so a future re-merge can be split again without the upsert branch);
   return stats `{components, new_project_ids, moved_applications}`.
8. **Transactionality**: one transaction per split, plus
   `SELECT ... FOR UPDATE` on the `projects` row (or an advisory lock on
   the project id) so a concurrent sync regroup can't interleave.

### Edge cases

- **Single component** (data changed since the request — e.g. a parent-hunt
  linked the roots): don't split; re-derive state; resolve the review with
  `resolution='obsolete'`; finish the request/job successfully with a note
  in `stats`.
- **Project no longer exists** (cascade deleted): log, mark job `done`,
  nothing else.
- **Job failure**: existing retry machinery applies (`MAX_ATTEMPTS 3`,
  10-min backoff); on final failure set `split_requests.status='failed'`
  with `error`, and leave the review pending so the user can retry.

## Frontend contract (what pam-client will do once shipped)

- The "Review details are not currently available" fallback disappears:
  every ambiguous project has a candidate list (Part 1). Until Part 1
  ships, pam-client derives candidates itself from `project_applications`
  (`has_parent_refs = false`) as a stopgap.
- The multi-root banner gains **"Split into separate schemes"** →
  `apply_grouping_resolution(review.id, 'split')`; on `split_queued` the UI
  shows "Split requested — the worker will separate these schemes shortly"
  and reads `split_requests` for the in-flight state.
- After completion, each new component is a normal project (own feed row,
  own lead pages); `unlinked_*` orphans keyed by `(authority, address_key)`
  resurface on each resulting project automatically — no extra work.
- `docs/grouping-review-ui.md` here gets its "splitting is not supported"
  note removed at that point.

## Acceptance criteria

1. Every `grouping_state='ambiguous'` project has exactly one pending
   `multi_root` review with a fresh candidate list; every pending
   `multi_root` review belongs to a currently-ambiguous project.
   ```sql
   select
     (select count(*) from projects p where p.grouping_state='ambiguous'
        and not exists (select 1 from grouping_review r
          where r.reason='multi_root' and r.status='pending'
            and r.project_id=p.id)) as ambiguous_without_review,
     (select count(*) from grouping_review r join projects p
        on p.id=r.project_id
      where r.reason='multi_root' and r.status='pending'
        and p.grouping_state<>'ambiguous') as stale_reviews;
   -- both 0
   ```
2. Splitting a 2-root project produces two projects, each with exactly one
   `is_root`, correct `n_applications`, and memberships whose reference
   edges never cross project boundaries:
   ```sql
   -- no membership pair (a cites b) split across projects
   select count(*) from project_applications a
   join project_applications b
     on b.project_id <> a.project_id
    and a.parent_ref is not null
    and b.uid = (select uid from applications
                 where authority = (select authority from projects
                                    where id = a.project_id)
                   and upper(reference) = a.parent_ref limit 1);
   -- 0 (adjust for suffix-stripped matching)
   ```
3. `split_requests` is empty and the review row is `resolved/'split'`
   within one worker poll of the request (~1 min when idle).
4. Retried/idempotent: clicking split twice, or splitting while a sync is
   running, yields one split and no orphaned jobs.

## Out of scope

- Automatic splitting (the queue exists precisely because these need human
  judgement).
- Choosing *which* component inherits UI stars/meta (documented default:
  the old root's component).
- `main_uid` headline scoring — orthogonal; rebased onto scheme projects
  separately (see `construction-timing/backend-spec-main-uid.md`).

## Implementation notes (2026-10-12, as built in `simoPh83/pam`)

Implemented: Part 1 in `pam/projects.py::_set_root_and_state` (new
`_sync_multi_root_review`), backfill `scripts/backfill_multi_root_reviews.py`;
Parts 2-3 in `scripts/apply_grouping_rpc.py`; Part 4 in
`pam/projects.py::split_project` + the `kind='split'` branch in
`pam/worker.py::process()`. Deviations from the text above, discovered while
implementing:

1. **Candidates are members with no *resolved* parent ref**, not literally
   `has_parent_refs = false` — a root that cites an unresolvable ref still
   has `has_parent_refs = true` but is a genuine candidate (this also matches
   what the migration's `classify()` actually wrote).
2. **`refresh()` does not recompute `address_key`** (it only recomputes
   `n_applications`, `latest_uid`, `name`, `first_seen`, `last_updated`,
   `root_in_db`). `split_project` sets each new project's `address_key` from
   the component's shortest member address instead; the retained project
   keeps its existing key.
3. **Single-component edge case**: the review row is left to
   `_sync_multi_root_review` after re-deriving state — resolved `obsolete`
   when the project really left `ambiguous`, refreshed in place if it
   genuinely still is (keeps acceptance check 1 at zero rather than leaving
   an ambiguous project with no review).
4. `split_requests` gains an explicit `status='running'` update while the
   worker executes, and the row is **deleted** on success (the "prefer
   delete" option), so the RPC's `ON CONFLICT ... WHERE status IN
   ('done','failed')` re-arm branch in practice only fires after a failure.
