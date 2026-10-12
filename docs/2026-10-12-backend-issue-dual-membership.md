# Backend issue: dual project memberships break the one-project-per-app invariant

**Date:** 2026-10-12 · **For:** `simoPh83/pam` (worker + schema)
**Found from:** pam-client — clicking a merged application's tab rendered it
as a standalone page (membership lookup ambiguous).

## Symptom

`Croydon/26/01368/FUL` is a member of **two** projects:

| project_id | n_applications | is_root | linked_by | created_at |
|---|---:|---|---|---|
| 88733 | 4 | false | reference | 2026-10-04 |
| 650582 | 1 (singleton, itself the root) | true | reference | 2026-10-10 (migration) |

The scheme model requires *every application is a member of exactly one
project* (verified at migration time), and the frontend relies on it.

## Scale (measured 2026-10-12)

```sql
select count(*) from (
  select uid from project_applications group by uid having count(*) > 1
) t;
-- 2,249 uids
```

Shape: 2,242 uids in exactly 2 projects (7 in 3+); 2,233 involve a
singleton project; **none of the involved projects were created on
2026-10-12** — this predates the split deploy and is not caused by it.
Duplicate rows are almost all `linked_by='reference'` (4,505 rows; only 2
`inferred`).

## Likely mechanism

At migration, `26/01368` was its own component → singleton project 650582.
Later, incremental `group()` resolved a reference edge connecting it to the
25/00586 chain and inserted a membership in 88733 **without removing the
existing membership** in 650582. The PK is `(project_id, uid)`, so
`ON CONFLICT DO NOTHING`-style upserts can't detect that the uid already
belongs elsewhere. `projects` aggregates (n_applications, root_uid) and any
per-project review rows then disagree with reality — 88733's
`multi_root` review listed 26/01368 as a candidate root while the worker
still considered it the root of 650582.

## Asks

1. **Repair script**: find dual memberships; keep the membership whose
   project contains the uid's *resolved reference neighbourhood* (or simply
   the non-singleton project when the other is a singleton whose root is the
   uid itself), delete the loser, re-derive the affected projects
   (`refresh()` + `_set_root_and_state()`), and retire now-empty projects
   (re-point `ui_*` rows / `missing_parents` / `project_aliases` as the
   migration did). 2,233 of 2,249 look like the easy singleton case.
2. **Enforce the invariant**: `create unique index on project_applications
   (uid);` — the schema should make dual membership impossible.
3. **Fix `group()`** in `pam/projects.py`: before inserting a membership,
   delete (or move) any existing `project_applications` rows for the uid in
   other projects, and re-derive the abandoned project (it may become empty
   → retire, or lose its root → re-run `_set_root_and_state`).

## Frontend mitigation (shipped in pam-client)

`getLeadDetail()` no longer uses `maybeSingle()` for the membership lookup
(which errored silently on duplicates → project context lost). It fetches up
to 5 rows and picks the project with the most members (ties → lowest id).
Remove the workaround once the unique index lands.

## Validation

```sql
-- must return 0 after repair
select count(*) from (
  select uid from project_applications group by uid having count(*) > 1
) t;
-- no empty projects left behind
select count(*) from projects p
where not exists (select 1 from project_applications pa
                  where pa.project_id = p.id);
```

## Implementation notes (2026-10-12, as built in `simoPh83/pam`)

1. **`group()` fix (ask 3):** before the membership upsert, `group()` now
   deletes the uid's rows under any *other* project id into a temp
   `_moves` table, moves that uid's `missing_parents` rows and pending
   `grouping_review` rows to the new project, then refreshes and retires
   the abandoned projects (new `_retire_projects()` helper: dependents —
   `missing_parents`, `project_aliases`, the five `ui_*` tables with
   per-table dedupe — follow to the project that absorbed the members, or
   the id is simply deleted when no successor is found). Verified on the
   `Croydon/26/01368/FUL` example: after `group()` the uid sits only in
   88733 and singleton 650582 is retired with no empty project left
   (rolled back).
2. **Repair script (ask 1):** `scripts/repair_dual_memberships.py`
   (dry-run default). Winner rule as asked — reference neighbourhood, then
   non-singleton, then lowest id; several reference winners mean the
   projects are merge candidates, so the lowest id wins. One pass resolves
   2-membership uids; the script loops (max 5 passes) because 3+-
   membership uids can shed only one row per pass. Dry-run result: 2,284
   dual uids → 2,291 rows deleted (1,315 by reference, 966 by size, 3 by
   lowest id), 2,269 empty singletons retired, `duals_after = 0`,
   `empty_projects_after = 0`.
3. **Invariant (ask 2):** the script finishes with
   `CREATE UNIQUE INDEX IF NOT EXISTS project_applications_uid_key ON
   project_applications (uid)` — but only when `duals_after = 0`, so it
   can't silently skip the index while reporting success.
4. **The pam-client `getLeadDetail()` mitigation can stay until the
   `--apply` runs**; after the unique index lands, `maybeSingle()` is safe
   again and the workaround can be removed.
