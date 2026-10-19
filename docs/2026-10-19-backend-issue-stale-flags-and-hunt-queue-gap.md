# Backend issue: stale chain flags + hunt-queue gaps (Haringey HGY/2024/0141)

Date: 2026-10-19 · Raised from: pam-client · Owner: PAM backend (`simoPh83/pam`)
Follow-up to: `docs/2026-10-14-backend-issue-root-in-db-vs-grouping-state.md`

## Symptom
Haringey `HGY/2024/0141` shows in the projects table as a complete scheme
(root + children icon, no "Incomplete chain" signals beyond the "Multiple
schemes" label), but its description — and those of at least 17 of the 41
members — cite `HGY/2022/1833` (permission dated 10 July 2023), which is not
in `applications`. `missing_parents` has **no row** for it even though the
Haringey parent hunt has run to completion.

## Case investigated: Haringey / HGY/2024/0141 (project id 109185)

`projects` row: `root_uid = Haringey/HGY/2024/0141`, `n_applications = 41`,
`grouping_state = 'ambiguous'`, `root_in_db = true`, `has_missing_ancestors = false`.
`grouping_review`: one pending `multi_root` row (2026-10-12, empty candidates).

- Root member `HGY/2024/0141`: app_type `Conditions`, role `discharge`,
  `parent_ref = 'HGY/2022/1833'`. Description: *"Approval of details (Phase 1A
  only) pursuant to Conditions 29 (Overheating) attached to Planning Permission
  Ref: HGY/2022/1833 dated 10 July 2023."*
- 16+ other members (`2025/…` CND/NMA) also cite `HGY/2022/1833`.
- `HGY/2022/1833` is absent from `applications`; the backend's own resolution
  rule (`upper(reference) = ref OR starts_with(upper(reference), ref || '/')`)
  returns 0 rows.
- `missing_parents`: no row for `(Haringey, HGY/2022/1833)`.
- Applications were first seen 2026-10-03; descriptions unchanged since ingest.

## Diagnosis (two distinct problems)

### 1. Stale flags — the row predates the flag fix
Dry-running the current `refresh()` on project 109185 (rolled back, no writes)
produces the correct values:

```
before: root_uid=Haringey/HGY/2024/0141, ambiguous, has_missing_ancestors=false, root_in_db=true
after : root_uid=Haringey/HGY/2024/0141, ambiguous, has_missing_ancestors=true,  root_in_db=false
```

So the fixed code is correct; this project's stored flags were simply never
re-derived by it. Odd, because `refresh_project_flags.py` evidently ran
(`has_missing_ancestors` has no NULLs globally, and spot-checked `ambiguous`
projects, e.g. Barnet 8685/8756/8904/9039/9082, match a fresh re-derivation).
Its filter (`root_in_db IS DISTINCT FROM true OR grouping_state <> 'clean'`)
should have included 109185 (`ambiguous`). Worth checking the script's run log
for skipped/failed batches.

### 2. Hunt-queue gap — `refresh()` never enqueues `missing_parents`
`missing_parents` rows are only inserted by `group()` (pass 4b), i.e. when an
application's membership is recomputed. These applications were grouped on
2026-10-03/04; the parser learned to extract this ref shape around 2026-10-12
(`pam/projects.py` `parent_refs_of`). Nothing re-ran `group()` for them after
the parser change, and `refresh()` — used by sync and by the flags script —
re-parses descriptions for the flags but does **not** enqueue unresolved refs.
Result: the Haringey hunt completed without ever seeing `HGY/2022/1833`, and
the project cannot heal itself.

**Scale:** 768 unresolved stored `parent_ref`s across **726 projects** have no
`missing_parents` row (first parsed ref per member only — the true count
including secondary parsed refs is higher):

```sql
with unresolved as (
  select distinct pa.project_id, a.authority, pa.parent_ref
  from project_applications pa
  join applications a on a.uid = pa.uid
  where pa.parent_ref is not null
    and not exists (
      select 1 from applications p
      where p.authority = a.authority
        and (upper(p.reference) = pa.parent_ref
             or starts_with(upper(p.reference), pa.parent_ref || '/')))
)
select count(*) filter (where mp.reference is null) as refs_without_queue_row,
       count(distinct u.project_id) filter (where mp.reference is null)
from unresolved u
left join missing_parents mp
  on mp.authority = u.authority and mp.reference = u.parent_ref;
```

## Requested backend changes

1. **Fix this project now**: targeted `refresh(conn, [109185])`, or re-run
   `scripts/refresh_project_flags.py` and investigate why 109185 was skipped.
2. **Close the queue gap**: make `refresh()` (or a one-off backfill script)
   `INSERT INTO missing_parents … ON CONFLICT (authority, reference) DO NOTHING`
   for refs that parse but don't resolve — same source list as
   `_set_root_and_state()`'s live resolution. For this case:
   `('Haringey', 'HGY/2022/1833', 2022, 'Haringey/HGY/2024/0141', 109185)`.
3. **Optional, longer term**: when the parser changes, consider a re-`group()`
   pass over applications whose stored `parent_ref` differs from a fresh
   `parent_refs_of(description)`, so queue and flags stay aligned after parser
   updates.

## Client impact
None. Once the backend re-derives the flags, the UI shows the amber
"Incomplete chain" icon (driven by `has_missing_ancestors`) for this project;
the parent-hunt tooltip will read "earlier application predates our records —
being fetched" once the queue row exists.

## Resolution (2026-10-19, backend)

1. **Queue gap closed in code.** `_set_root_and_state()` (called by
   `refresh()`) now inserts every member ref that parses but does not resolve
   into `missing_parents … ON CONFLICT (authority, reference) DO NOTHING` —
   the same live resolution the flags use, so the queue can no longer drift
   behind the flags when a project is refreshed without a `group()` pass.
   `requested_by`/`project_id` are set as usual; existing rows (including
   `exhausted` verdicts) are preserved.
2. **Backfill run** (`scripts/backfill_hunt_queue.py`, committed): refreshed
   all 4,225 projects carrying an unresolved stored `parent_ref`.
   - Project 109185 healed exactly as predicted: `has_missing_ancestors
     false→true`, `root_in_db true→false`; queue rows inserted for
     `HGY/2022/1833` (requested_by `Haringey/HGY/2024/0141`) and
     `HGY/2016/2616`; the stale empty `multi_root` review row was refreshed
     to its real 39 candidates.
   - Queue gap: 768 refs / 726 projects without a queue row → **0 real
     remainder** (see 3).
   - Stale flags: **384 projects** (not just 109185) carried
     `has_missing_ancestors=false` despite an unresolved stored ref (140
     ambiguous, 194 provisional, 50 clean — the clean ones were never
     covered by `refresh_project_flags.py`'s filter). All re-derived.
3. **Known residual (= item 3 of "Requested backend changes").** 20 stored
   refs still have no queue row: 13 no longer parse from their members'
   descriptions with the current parser (stale stored `parent_ref`; flags
   are already correct per the live parse), and 7 (`Kensington PP/2x/…`)
   still parse but only as *secondary* refs on members whose stored
   `parent_ref` resolves — invisible to a stored-ref-scoped backfill.
   Closing this needs a full fresh-parse sweep (or the re-`group()` pass of
   item 3); both also clean up the 29 probe-visible "stale flag" artifacts,
   which are the same stale stored refs, not live-flag errors.
4. **Why 109185 was skipped — not fully reconstructable.** The run log of
   the original `refresh_project_flags.py` run isn't in the repo, but the
   project's pending `multi_root` row still had *empty* candidates, i.e. no
   fixed-code `refresh()` had touched it since 2026-10-12. The 140
   ambiguous stale projects prove the script's run was incomplete or
   predated a state change (`regroup_schemes.py` re-roots projects while
   deliberately leaving chain flags to a later refresh; the 10-14
   purge/re-backfill also ran before the flag fix landed at 22:01).
5. **Hunts are not auto-enqueued** (`AUTO_PARENT_HUNT` off): run
   `python -m pam.worker enqueue-parent-hunt` to start fetching the ~750
   newly catalogued refs (PlanIt etiquette floor ≈ 1 lookup/minute).
6. **Pseudo-ambiguity fixed (follow-up, same day).** Healing 109185 exposed
   that its `multi_root` review row listed 39 "candidates" — all discharge/
   amendment *children* of the same missing parent, zero originals. Root
   election in `_set_root_and_state()` now treats a project whose parentless
   members are all non-originals citing only unresolved refs as **awaiting
   the parent hunt**: the earliest citer is elected as provisional root
   (`provisional`, amber incomplete-chain) and no `multi_root` review is
   raised; a found parent re-groups the children and re-forms the structure,
   an exhausted one leaves a truthful provisional chain. Genuine competing
   origins (multiple originals, or parentless members citing nothing) still
   go `ambiguous`. For 109185: `ambiguous → provisional`, review row
   auto-resolved `obsolete`; the Barnet genuinely-ambiguous controls were
   verified unchanged. Also closed the residual queue gap with a full
   fresh-parse sweep (`scripts/sweep_fresh_parent_refs.py`): 355 members
   whose stored refs disagreed with the current parser were re-grouped and
   the final 73 unqueued refs catalogued (incl. the Kensington `PP/2x/…`
   secondary refs).
