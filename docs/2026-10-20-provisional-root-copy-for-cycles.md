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

## Investigation notes for later

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
