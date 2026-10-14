# Backend issue: `root_in_db` and `grouping_state` disagree (Enfield 24/02608/FUL)

Date: 2026-10-14 · Raised from: pam-client · Owner: PAM backend (`simoPh83/pam`)

## Symptom
In the projects table, the first-column icon "Scheme root missing" and the
"Incomplete chain" label are expected to mean the same thing, but they are two
independent columns on `projects`:

| UI element | Source |
|---|---|
| Icon "Scheme root missing" | `projects.root_in_db = false` |
| Label "Incomplete chain" | `projects.grouping_state = 'provisional'` |

The client does no computation; it displays both columns as-is
(`components/projects-table.tsx`). A case exists where the icon shows without the
label (not yet investigated; see "Follow-up").

## Case investigated: Enfield / 24/02608/FUL (project id 719764)

`projects` row: `root_uid = Enfield/24/02608/FUL`, `n_applications = 15`,
`root_in_db = false`, `grouping_state = 'provisional'`, `address_key = 'ALMA ESTATE EN3'`.

Contradiction: `root_uid` points at an application that **is** in `applications`
(is_root = true, role `original`, `parent_ref` null, `has_parent_refs` false; the
description is a full FUL referencing no other application), yet `root_in_db = false`.

Members of the project (`project_applications`):
- `24/02608/FUL` — root, original.
- 12 × `26/…/CND` and `26/00043/CND` etc. — role `discharge`, `parent_ref = 24/02608/FUL`.
- `22/02876/CND` — role `variation`, `parent_ref = 15/02039/OUT` (not in DB).
- `25/04074/CND` — role `variation`, `parent_ref = 19/03624/VAR` (not in DB).

`missing_parents` for this project: `15/02039/OUT`, `17/04670/VAR`, `19/03624/VAR`
— all `status = 'pending'`, `attempts = 0`, `found_uid` null. `grouping_review`: no rows.

## Diagnosis
The chain is incomplete because **older ancestors** (2015 outline, 2017/2019 VARs)
referenced by member applications are not fetched yet — not because
`24/02608/FUL` is missing. `root_in_db = false` is therefore either stale or is
being used to mean "an ancestor of the chosen root is missing", which the UI (and
its hint text "Original application predates our records") reads as "the root row
is missing".

## Requested backend changes
1. **Define `root_in_db` strictly**: `true` iff `root_uid` exists in `applications`.
   For this project it should be `true`.
2. **Carry "chain incomplete" separately** (e.g. `has_missing_ancestors boolean`,
   or derive from pending/unfound `missing_parents` rows for the project). The UI
   can then show the icon/label from the right signal. Tell us the final column
   name and we'll update the client.
3. **Decide root election**: once `15/02039/OUT` (or the other ancestors) is
   fetched, should the project root be re-elected to the oldest ancestor? Today the
   root is the `24/02608/FUL` row while three ancestor refs sit pending.
4. **Define the relationship between the flags**: is `provisional` meant to be a
   superset of "any pending missing_parents"? Ensure the two are computed from the
   same source so they cannot diverge.
5. Check why the `missing_parents` rows have `attempts = 0` — they have not been
   tried yet, so the project may resolve itself once the worker reaches them.

## Follow-up (not yet done)
The reverse case — icon shown, label absent (`root_in_db = false` and
`grouping_state <> 'provisional'`). Suggested query:

```sql
select grouping_state, root_in_db, count(*)
from projects
group by 1, 2
order by 1, 2;
```

and list examples where `root_in_db = false and grouping_state <> 'provisional'`,
plus rows where `root_in_db = true` but the root uid is absent from `applications`.
Share the cases found so the client can be adjusted once the semantics are fixed.

---

## Resolution (2026-10-14, PAM backend)

### Root cause

The client's diagnosis is correct. `root_in_db` never meant "the `root_uid`
row exists in `applications`" — it was computed in `projects.refresh()` as
"no member of the project has a `parent_ref` with a non-`found` row in
`missing_parents`" (matching the original spec,
`docs/2026-10-05-backend-spec-new-features.md` §3). Two defects followed:

1. **Stale source.** `missing_parents` rows were only ever deleted by the
   parent hunt (`mark_found`) or retention. When a cited parent arrived via
   ordinary sync/backfill, `grouping_state` healed on the next refresh (it
   re-resolves refs live) but the queue row lingered, leaving
   `root_in_db = false` on otherwise-clean projects — possibly forever for
   `exhausted` rows (never retried, never re-checked locally).
2. **Asymmetric inputs.** The `root_in_db` join read only the stored
   `pa.parent_ref` (the *first* parsed ref), while `grouping_state`'s
   unresolved check covers *all* parsed refs plus `manual_parent_refs`.

Live data at time of fix — cross-tab of `grouping_state × root_in_db`:

| grouping_state | root_in_db | count |
|---|---|---|
| clean | true | 81,353 |
| provisional | true | 4,963 |
| ambiguous | true | 2,117 |
| provisional | false | 1,959 |
| ambiguous | false | 579 |
| clean | false | **4** ← the true contradictions |

- `root_in_db = true` with `root_uid` absent from `applications`: **0 rows**
  (structural: the root is elected from members inner-joined to
  `applications`, so a strict "root row exists" flag would be constant true).
- `root_in_db = false` with the root present (the Enfield 719764 pattern):
  **2,542 projects** — i.e. the flag's de-facto meaning was "chain
  incomplete", exactly colliding with `provisional`.
- The 4 `clean` + `root_in_db = false` projects (the follow-up's
  icon-without-label case) all had a stale `missing_parents` row whose ref
  **now resolves** in `applications`: Enfield 714121 (pending; the cited
  `25/02958/FUL` arrived via sync and is now the project's own root) and
  Tower Hamlets 139672 / 138916 / 139655 (`exhausted`; the parent later
  arrived with a `/A1`-style suffix that the local-resolution rule matches).
  20 such stale rows existed globally (15 exhausted, 5 pending).

### Answers to the numbered requests

1. **Strict `root_in_db`** — not implemented as proposed: it would be a
   constant `true` (the root is always elected from stored members). See 2.
2. **New column: `projects.has_missing_ancestors` (boolean)** — true iff any
   member cites a ref (parsed or manual) that does not resolve to a stored
   application. Derived **live** in `_set_root_and_state()` alongside
   `grouping_state`, from the same data — the two cannot diverge.
   `root_in_db` is kept for backwards compatibility and is now exactly
   `NOT has_missing_ancestors`; both are safe for the client to read, but
   please migrate to `has_missing_ancestors` (positively named, matches the
   "chain incomplete" meaning). Suggested mapping: icon "Scheme root
   missing" ← `has_missing_ancestors`; label "Incomplete chain" ←
   `grouping_state = 'provisional'` (unchanged).
3. **Root re-election** — already automatic, no change needed. When the hunt
   (or a sync) lands `15/02039/OUT`, `mark_found` → `projects.group()` →
   `refresh()` elects the unique member with no resolved parent ref — the
   2015 outline — and the state flips to `clean` once nothing else is
   unresolved.
4. **Flag relationship** — fixed by construction: both flags are now set in
   the same statement from the same live `has_unresolved` computation.
   `clean` ⇒ `has_missing_ancestors = false`; the converse is not
   guaranteed (a project can be `provisional` for other reasons, e.g. no
   role-`original` root candidate).
5. **`attempts = 0`** — `attempts` is only incremented on a *failed* API
   lookup; a success deletes the row. `attempts = 0` + `pending` = never
   tried. Hunts are not automatic (`AUTO_PARENT_HUNT` defaults off). A
   parent-hunt job per authority (incl. Enfield #48218) has been queued
   since 2026-10-12 at priority 50, behind ~550 priority-70 backfill jobs —
   it will drain the whole Enfield queue (246 refs) in one run once reached;
   the 2015/2017/2019 ancestors sit mid-queue (86 newer refs ahead).

### Changes shipped

- `pam/projects.py`
  - Migration: `projects.has_missing_ancestors BOOLEAN` (nullable; backfilled
    by the script below).
  - `refresh()` now **self-heals** the hunt queue: deletes `missing_parents`
    rows tied to the refreshed projects whose ref already resolves in
    `applications` (same suffix-tolerant rule as the hunt's local-first
    lookup). Prevents the stale-row divergence from recurring.
  - `_set_root_and_state()` sets `root_uid`, `grouping_state`,
    `has_missing_ancestors` and `root_in_db` in one statement from live
    resolution.
- `scripts/refresh_project_flags.py` (new, idempotent, `--dry-run`):
  heals stale queue rows globally, re-derives flags for every project with
  `root_in_db IS DISTINCT FROM true OR grouping_state <> 'clean'`, backfills
  `has_missing_ancestors = false` on the untouched clean remainder.
- `scripts/find_missing_parents.py` / `scripts/regroup_schemes.py`: removed
  the duplicated old-semantics `root_in_db` SQL; flags are left to
  `refresh()` / the new script.

### Deploy sequence

1. Merge + push; Railway redeploys the worker (migration is applied by
   `ensure_schema()` on start). **The worker must be restarted** — running
   processes keep the old module.
2. `python scripts/refresh_project_flags.py --dry-run`, then without
   `--dry-run` (one-off heal + re-derivation).
3. Optionally bump the queued parent-hunt jobs (ids 48212–48218) above
   priority 70 if the ancestors should be fetched before the backfill wave
   drains; otherwise they run when the queue reaches them.
4. Client: switch the icon to `has_missing_ancestors` at leisure;
   `root_in_db` remains correct in the meantime.
