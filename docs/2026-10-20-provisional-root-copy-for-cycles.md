# Follow-up: provisional-root UI copy is wrong for reference-cycle projects

Date: 2026-10-20 · Status: noted, not yet fixed · From: cycle-fallback heal
(2026-10-19, `scripts/heal_cyclic_projects.py`; see
`2026-10-10-scheme-grouping-spec.md` `_set_root_and_state` bullet, shape (b)).

## Symptom

Greenwich 24/3427/L and 24/3426/F (the Devonport House sibling pair) now show:

> No root identified — This scheme has a provisional root: none of its
> applications looks like an original, and none references an earlier
> application.

This is doubly inaccurate for cycle projects:

1. **A root IS identified** — the cycle fallback elects the earliest member
   (24/3427/L in project 101405, 24/3426/F in project 652203) and sets
   `root_uid` / `is_root` as usual.
2. **The applications DO reference another application** — they cite each
   other ("linked to the ... application under ref: …") and both refs
   *resolve*. The copy only fits the reference-less provisional case.

## Why the frontend can't do better today

`projects.grouping_state = 'provisional'` now covers several distinct shapes
with no machine-readable way to tell them apart:

| Shape | Root basis | Flags |
|---|---|---|
| single non-original root, refs resolve | natural | `root_in_db=true`, `has_missing_ancestors=false` |
| awaiting parent hunt | earliest citer | `has_missing_ancestors=true` |
| **reference cycle / cross-project fragment** | **earliest member (elected)** | `root_in_db=true`, `has_missing_ancestors=false` |

The UI copy was written for the first two; the third (new, ~300 projects
healed 2026-10-19) falls through to a generic message.

## Investigation (2026-10-20) — measured, backend change NOT needed

The "No root identified" copy lives in pam-client
(`components/project-grouping-review.tsx` ~L102 and
`components/projects-table.tsx` ~L280), keyed on
`grouping_state === 'provisional' && !has_missing_ancestors`. That branch
conflates two shapes. Measured on the live DB:

| provisional, no missing ancestors | count | root | refs |
|---|---|---|---|
| **cycle / cross-project fragment** (every member ref resolves) | 558 | earliest member, **elected** | all resolve |
| **reference-less** (nothing cites anything) | 586 | discharge 433 / nma 61 / variation 92 | none resolve |

So the copy is wrong for **558** projects — every one has a root *and* every
application references a resolving earlier one. The reference-less shape
(586) has no original root either (all roots are discharge/nma/variation),
so even there "none of its applications looks like an original" is accurate
but "none references an earlier application" is the operative truth.

**Conclusion: no schema change, no new backend column.** The distinction is
already fully derivable from data the UI reads — a provisional project whose
members all have `has_parent_refs = true` (and no missing ancestors) is the
cycle shape; one with all `has_parent_refs = false` is the reference-less
shape. `getProjectBundle` already returns members with `has_parent_refs`.
A persisted `root_basis` column would only duplicate this. Recommended fix
is a small copy-only change in pam-client.

## UI fix (proposed, copy-only, pam-client)

Replace the single provisional block with a shape-aware one. Badge stays
"No root identified" for the reference-less shape; the cycle shape gets a
distinct badge + copy, e.g. badge "Cross-referenced applications", copy
"These applications reference each other (e.g. parallel consents for the
same works); the earliest is shown as the provisional root." Derivation in
`getProjectBundle` or inline:
`const allRef = members.length > 0 && members.every(m => m.has_parent_refs)`.
Two files: `components/project-grouping-review.tsx`, `components/projects-table.tsx`.

Note: the cycle derivation must tolerate members lacking `has_parent_refs`
visibility — confirm `getProjectBundle`'s member shape includes it before
implementing; if not, fall back to `member.parent_ref != null`.

## Parked / notes

- Backend option: persist a `root_basis` / provisional-reason column
  (`natural` | `awaiting_hunt` | `cycle_elected`) in
  `_set_root_and_state()` so the UI can pick accurate copy, e.g.
  "provisional root: applications cross-reference each other
  (parallel consents); earliest shown as root".
- Cheaper UI-side approximation: members' `has_parent_refs` / `parent_ref`
  are already readable — "all refs resolve" + provisional ⇒ cycle/fragment.
  But an explicit backend field is more truthful.
- Related infelicity (same session): cycle members keep `role='discharge'`
  (the Full and the LBC both), which is why "none looks like an original"
  is technically true. Decided 2026-10-19 not to add a parser guard for the
  "linked to …" phrasing; a role touch-up for sibling consents (e.g.
  `parallel_consent`) is a possible future refinement but NOT needed for
  correctness.
- Also parked: Devonport remains two provisional projects on one address
  key (24/3426/F's 26-member project + the LBC's 6-member one); merging
  would mean reviving the superseded `merge_linked` logic — separate
  decision.
- Survey evidence (172 citation cycles, classification into same-works
  consents vs linked distinct sites): session artifact
  `files/cross_ref_cycles.csv` from the 2026-10-19 investigation.
