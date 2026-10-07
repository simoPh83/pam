# Worker fix spec — `last_updated` pollution from parent-hunt/backfill inserts

> **STATUS (2026-10-07, implemented).** Final semantics, which supersede the
> proposals below where they differ:
>
> - `applications.last_updated` = date of the latest **PlanIt event**, never
>   the date we noticed it.
>   - **Insert:** `min(max(start_date, decided_date), today)`, else today.
>   - **Update** (a tracked field changed): `GREATEST(existing, event)` where
>     event = `decided_date`, else PlanIt `last_different`, else `start_date`,
>     else today (capped at today). Never moves backwards.
> - `projects.last_updated` = `max(member.last_updated)` (via
>   `projects.refresh()`). `projects.latest_uid` is ordered by
>   `coalesce(decided_date, last_updated)`, so "latest application" and
>   "project last_updated" can legitimately differ.
> - The §2 cleanup SQL was run, but with `state_history.observed_at`
>   **replaced** by `coalesce(decided_date, last_different::date)` because
>   `observed_at` is the detection date (the same pollution). Run in batches
>   of 5,000 uids with commits.
> - `first_seen` unchanged (discovery date, debugging only).
>
> ## Retention policy (rolling window)
>
> `pam/retention.py::prune(conn, months=RETENTION_MONTHS)` (default **16**,
> env `RETENTION_MONTHS`). Runs automatically **after every successful sync**
> and **before every parent_hunt**; manual: `python -m pam.worker prune`.
>
> 1. Delete projects with `last_updated < today - N months` (FK cascade clears
>    `project_applications`, aliases, stars, meta). Projects that are starred
>    or annotated are **kept**.
> 2. Delete applications no longer in any project (ungrouped ones get 7 days
>    grace), `missing_parents` rows whose project is gone, and `state_history`
>    for deleted applications.
>
> Rationale (measured on the 2026-10-07 backup): 1y = 66.7k projects/147k
> apps; 16m = 84.7k/178k; 2y = 119k/233k. 16 months leaves room for pending
> parent-hunt roots (10k pending refs) while staying well under the free-tier
> 500 MB. Caveat: `ui_outreach` has no project FK and is not protected.
> Widening the window means re-importing from a CSV backup
> (`data/backup-2026-10-07/`, gitignored) or re-fetching.
>
> ## Runbook: DB full / project read-only (Supabase free tier)
>
> Symptoms: writes fail, `default_transaction_read_only = on`, disk ~99%.
> Disk cannot be resized on the free plan.
>
> 1. **Avoid the cause:** never run big single-transaction UPDATE/DELETE on
>    `applications`. Every rewritten row leaves a dead tuple and WAL; batch
>    (≤5k rows), commit per batch. Stop the worker first (it holds locks).
> 2. Use the **direct** connection (`DATABASE_URL`, port 5432); the pooler
>    (6543) may refuse. Per session: `SET default_transaction_read_only = off`.
> 3. `DELETE`/`UPDATE`/plain `VACUUM` do **not** shrink files and
>    `VACUUM FULL` needs free disk. Only `TRUNCATE` reclaims space.
> 4. Recovery that worked: export every public table to CSV (verify counts)
>    -> `TRUNCATE applications` -> delete stale projects -> `COPY` back only
>    the kept rows -> `ANALYZE`.
> 5. WAL (up to ~1 GB, `max_wal_size` 1024 MB) cannot be checkpointed by the
>    non-superuser role; it recycles by itself. A project restart does not
>    clear it. If still stuck, contact Supabase support or upgrade.
> 6. Check afterwards: `select pg_size_pretty(pg_database_size(current_database()))`.

**Date:** 2026-10-07 · **For:** `simoPh83/pam` (worker) · **From:** `pam-client`
session (see `docs/session-log.md` there)

## Problem

The web client's leads page filters/sorts projects by "latest PlanIt activity
in the chain" (child filed, or status change of any member). Requirement from
the user: *the date we found/scanned an application must not count as a
project update.*

Measured on 2026-10-07 — filter "updated since 2026-10-01" matches **108,957
of 160,156 projects**. Breakdown:

| Date | projects.last_updated | applications.last_updated | real state changes (state_history) |
|---|---|---|---|
| Oct 6 | 12,032 | 19,482 | 22 |
| Oct 5 | 45,641 | 79,351 | 0 |
| Oct 4 | 50,233 | 84,536 | 129 |
| Oct 3 | 37,623 | 77,051 | 42 |

Of the 18,659 applications **inserted** on Oct 6, only 1,981 were filed in
2026 — the rest are historical applications (2025 and older, back to 2003)
inserted by the **parent-hunt bootstrap** / backfill. Each insert stamps
`last_updated = today`, which bubbles up to `projects.last_updated`
(verified: `projects.last_updated = max(member.last_updated)`).

So a 2019 root discovered by the parent-hunt today marks its project as
"updated today" — wrong per the semantics above. The bulk pollution fades
from a 90-day window by ~January, but the parent-hunt has **10,075 refs still
pending**, so the trickle continues for weeks and keeps mis-dating ~10k
projects.

## What is NOT the problem

`Ledger.upsert` already implements "bump `last_updated` only when a tracked
field changed" for existing rows (verified in `pam/ledger.py`). Legit
re-scrape changes stamping "today" are fine — the change is detected within
a day or two of the real event. The bug is only the **INSERT path** for
applications whose PlanIt activity is historical.

## Proposed fix

### 1. Insert path: stamp the PlanIt event date, not the discovery date

When `Ledger.upsert` (or the parent-hunt insert path) inserts a **new** row,
set:

```
last_updated = greatest(coalesce(start_date, '-infinity'),
                        coalesce(decided_date, '-infinity'),
                        ...)          -- greatest() ignores NULLs in Postgres;
                                       -- use coalesce to handle all-null
```

i.e. the latest PlanIt-sourced event date on the record; fall back to
`today` only when neither date exists. This is transparently correct for
genuine new filings too (their `start_date` ≈ today), so no special-casing
the parent-hunt is needed.

Check `pam/projects.py` as well: if the grouping pass bumps
`projects.last_updated` on membership changes (rather than recomputing from
members), newly-discovered historical members will still pollute the project.
`projects.last_updated` should be `max(member.last_updated)` — recompute, not
`greatest(existing, today)`.

### 2. One-off cleanup: recompute polluted values

```sql
-- per-application "real" last-activity date
with real_activity as (
  select a.uid,
         greatest(
           coalesce(a.start_date,  '0001-01-01'::date),
           coalesce(a.decided_date,'0001-01-01'::date),
           coalesce((select max(sh.observed_at)::date
                     from state_history sh where sh.uid = a.uid),
                    '0001-01-01'::date)
         ) as d
  from applications a
)
update applications a
set last_updated = case
  when ra.d > '0001-01-01'::date then ra.d
  else a.first_seen              -- no signal: fall back to discovery date
end
from real_activity ra
where a.uid = ra.uid
  and a.last_updated > case when ra.d > '0001-01-01'::date then ra.d
                            else a.first_seen end;

-- then roll up
update projects p
set last_updated = coalesce((
  select max(a.last_updated)
  from project_applications pa
  join applications a on a.uid = pa.uid
  where pa.project_id = p.id), p.last_updated);
```

Caveat: legit non-state tracked-field changes (e.g. `n_documents`) have no
event date anywhere, so their recent bumps get rewritten to the last
state/file/decision date — acceptable: those changes don't matter for
"project activity" anyway. Run inside a transaction; idempotent-ish (safe to
rerun after the next parent-hunt batch).

### 3. Keep `first_seen` semantics unchanged

`first_seen` stays "when we discovered it" — useful for debugging and for the
spec's note that it is NOT display-quality.

## Acceptance criteria

1. Re-run a parent-hunt batch over ~100 known historical refs → affected
   projects' `last_updated` becomes the parent's `start_date`/`decided_date`,
   not the run date.
2. After the cleanup SQL: `select count(*) from projects where last_updated
   >= current_date - 7` drops from ~109k to the order of hundreds (daily sync
   volume + real decisions).
3. New genuine filings (next nightly sync) still appear in "updated in the
   last N days" immediately.
4. Client regression check: `pnpm verify:db` unaffected; the
   `projects_feed`-based leads page "Updated from" filter returns sensible
   counts.
