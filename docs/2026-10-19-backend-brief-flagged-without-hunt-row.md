# Backend brief: projects flagged with missing ancestors but no hunt row

Date: 2026-10-19. Follow-up to
[2026-10-19-backend-issue-stale-flags-and-hunt-queue-gap.md](./2026-10-19-backend-issue-stale-flags-and-hunt-queue-gap.md).

## Summary

644 projects have `grouping_state = 'provisional'`, `has_missing_ancestors = true`
and **no `missing_parents` row**. The UI shows the "incomplete chain" icon but
has no hunt status to explain it. They split into two groups:

| Group | Projects | Cause |
|---|---|---|
| A | 603 | A member cites a `parent_ref` that is not in `applications`, but no `missing_parents` row was ever queued (the hunt-queue gap). |
| B | 40 | Flag is set, yet no member has an unresolved `parent_ref`. Likely a stale flag. |

The 644 figure is from a live check on 2026-10-19. Counts will drift as sync runs.

## Group A — 603 projects: enqueue the missing hunts

Each of these has at least one `project_applications.parent_ref` that matches no
application (same authority, `upper(reference) = parent_ref` or prefix
`parent_ref || '/'`), and no `missing_parents` row for it.

Requested:
1. Backfill: insert a `pending` `missing_parents` row for each such
   `(authority, parent_ref)` with `requested_by` = the citing child uid and
   `project_id` set.
2. Make `refresh()` enqueue unresolved refs so the gap cannot reopen
   (already described in the linked doc, section 2).

Acceptance: after the backfill and one hunt cycle, every project in group A has
either a `pending` or `exhausted` row, or the flag cleared because the parent
was found.

## Group B — 40 projects: re-derive the flag

All 40 have an address key but no member with an unresolved `parent_ref`, so the
flag has nothing to point at.

Requested:
1. Re-run `refresh()` (or `refresh_project_flags.py`) on these projects and
   report which flag values change.
2. If any keep `has_missing_ancestors = true` after refresh, identify what the
   flag is based on, since it is not an unresolved stored `parent_ref`. A parsed
   description ref not stored on the member row is one possibility.

Acceptance: for every project, `has_missing_ancestors = true` implies at least
one unresolved ref exists, and a `missing_parents` row exists for it.

## Related inconsistency (not part of this fix, flag for awareness)

127 projects have `root_in_db = true` but still carry a `missing_parents` row
(91 clean, 22 ambiguous, 14 provisional with `exhausted`; 4 provisional with
`pending`). If the root is in the database the row should probably be removed.
The UI currently ignores neither case, so these show a "root missing" banner
that is likely wrong.

## Check queries

Group A and B membership:

```sql
with t as (
  select p.id
  from projects p
  where p.grouping_state = 'provisional' and p.has_missing_ancestors
    and not exists (select 1 from missing_parents m where m.project_id = p.id)
)
select
  exists (
    select 1 from project_applications pa join applications a on a.uid = pa.uid
    where pa.project_id = t.id and pa.parent_ref is not null
      and not exists (select 1 from applications p2 where p2.authority = a.authority
        and (upper(p2.reference) = pa.parent_ref
             or starts_with(upper(p2.reference), pa.parent_ref || '/')))
  ) as group_a,
  count(*)
from t group by 1;
```

## Work performed (2026-10-19)

- Extended `scripts/backfill_hunt_queue.py --target-brief` to backfill
  unresolved stored refs for this cohort, refresh Group B, and report refs
  still supporting a true flag. The ordinary all-unresolved backfill mode
  remains available.
- Ran the targeted repair while the worker was paused. Group B: 1 of the
  original 40 projects had stale flags and was corrected
  (`root_in_db: false → true`, `has_missing_ancestors: true → false`);
  39 remain flagged because the current parser finds unresolved refs in their
  descriptions. All 77 project/member-ref records found for those projects
  have hunt rows (63 exhausted, 14 pending).
- Group A currently has 601 projects and 376 distinct unresolved stored refs.
  Every ref has a `pending` or `exhausted` row (229 distinct refs pending,
  147 exhausted); there are no missing rows at the `(authority, reference)`
  level. The apparent project-level gap remains because the unique key allows
  only one `missing_parents` row per ref, while 601 projects cite those 376
  refs; rows are attached to a different citing project in many cases. The
  client contract says to resolve hunt status through each project's
  unresolved refs, not by `project_id` alone. If the live UI filters only by
  `project_id`, that join needs a client-side correction or a separate
  many-to-many association model; inserting duplicate hunt rows is not valid.
- The repair also found and healed one related residual outside the original
  cohort: project `634731` (`Kensington/CL/25/02999`) had a live parsed,
  unresolved `PP/25/00753` ref but a stale false flag and no hunt row. It is
  now provisional with `has_missing_ancestors = true` and a pending hunt row.
- Post-commit verification: zero unresolved stored refs without a hunt row,
  zero stale false flags for unresolved stored refs, and no Group B parsed
  refs without `pending`/`exhausted` hunt status. The worker remains paused.
- Hunts need no manual enqueue: `AUTO_PARENT_HUNT=1` on Railway, and 32
  `parent_hunt` jobs (one per authority with pending refs, incl. all
  authorities touched here) are already queued from today's cycle; the
  pending refs catalogued by this repair are picked up by those jobs as soon
  as the worker restarts. Note: one Kensington job (id 48215) is stuck in
  `running` from 2026-10-12 — `requeue_interrupted()` will flip it back to
  `pending` on worker start.
