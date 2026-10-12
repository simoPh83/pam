# Backend note: `unlinked_no_scheme` queue — standalone mains + parser gaps

**Date:** 2026-10-12 · **For:** `simoPh83/pam` (worker + schema)
**Related:** `2026-10-12-multi-root-split-backend-spec.md` (same repo, docs).
Short companion note, not a full spec — two worker-side issues found while
reviewing the queue from pam-client.

## Context (measured 2026-10-12)

The `unlinked_no_scheme` queue is **742 pending rows** (down from 2,094 at
migration — the parent-hunt is absorbing them steadily). App-type breakdown:

| app_type | rows |
|---|---:|
| Conditions | 475 |
| Full | 120 |
| Amendment | 107 |
| (null) | 14 |
| Outline | 14 |
| Heritage / Other | 12 |

Only **30 distinct cited parent refs** exist across the queue (strict
regex), and **none resolve** to a stored application — so the queue is
genuinely "parents we don't hold yet", not a local linking failure.

## Issue 1 — genuine standalone mains sit in the queue

Per the scheme-grouping model, a parent-less **original** application is a
legitimate root: it should become its own clean project, never an
`unlinked_no_scheme` row. Yet the queue contains clear standalone mains,
e.g.:

- `Bromley/26/03662/HPA` — "Single storey rear infill extension,
  installation of air condition unit…"
- `Brent/26/2322` — "Installation of a new mechanical extraction and
  ventilation system…"
- `Barnet/26/3230/FUL` — "Installation of a kitchen extract ventilation
  system within existing commercial unit…"

No parent ref, no discharge/variation/NMA keywords, no scheme at the
address. Expected: standalone clean project. Observed: queue row. Either
these predate the current grouping logic (migration/orphans_to_review
leftovers the worker never revisits) or `group()` queues no-ref
applications at scheme-less addresses regardless of classified role.

**Ask:** in `pam/projects.py::group()`, when a no-ref application at a
scheme-less address classifies as `original`, create a standalone project
(root = itself) instead of an `unlinked_no_scheme` review row. Also sweep
existing queue rows: any whose description classifies as `original` should
be promoted to a project and the review row auto-resolved (`obsolete`).

Caveat: PlanIt's `app_type` is the *form*, not the scheme role — most
`Full`/`Outline` rows in the queue are actually s73 variations, reserved
matters, or details records, so classification must stay
description-keyword based, not app_type based.

## Issue 2 — parent-ref parser misses common citing formats

Queue rows (and possibly grouped members) cite parents in formats the
strict regex
(`(?:permission|application|consent)\s*(?:ref…)?`) doesn't match:

- `Enfield/26/03528/RM` — "reserved matters approved under **Ref:**
  23/02239/OUT"
- Westminster s73s — "planning permission dated 16th June 2025 **(RN
  24/05892/FULL)**", "(RN: 25/03133/FULL)"
- `Wandsworth/2026/2752` — "planning permission dated 22/05/2025 **ref
  2025/1231**"

**Ask:** extend the parser with bare `Ref:` / `RN` / `RN:` prefixes
(still requiring a digit and min length, as now). Each fix lets `group()`
link the child to an already-stored parent with no API call — cheap wins,
same "resolved_locally" path the parent-hunt uses. Given only ~30 cited
refs exist in the current queue, impact today is small, but the same
patterns appear in grouped memberships and would improve future ingestion.

## Validation

- After the sweep: `unlinked_no_scheme` pending count drops by the number
  of `original`-classified rows; each promoted row leaves a clean
  single-application project.
- Parser change: re-run the existing ref-resolution probe and confirm the
  sample uids above link to their parents; no regression on the strict
  regex's false-positive guards.

## Implementation notes (2026-10-12, as built in `simoPh83/pam`)

1. **Issue 1 — the worker side already behaves as asked.** `group()` never
   creates `unlinked_no_scheme` rows: a no-ref application with no resolvable
   parent and no address bucket gets its own project (`target_project`'s
   final `INSERT`), and `_set_root_and_state` derives `clean` for a lone
   original. The queued standalone mains were migration /
   `orphans_to_review` leftovers, so no `group()` change was needed. The
   sweep is `scripts/promote_standalone_originals.py` (dry-run by default):
   `original`-classified pending rows go through `group()` (joining an
   address scheme if one has appeared since) and their review rows are
   auto-resolved `obsolete`; rows whose uid was manually assigned meanwhile
   are just resolved.
2. **Issue 2** — implemented as a separate `REF_PREFIX_RE` in
   `pam/projects.py` (bare `ref`/`refs`/`RN` with optional `:`/`.` then the
   narrow `_REF_SHAPE`), added to the `parent_refs_of` regex set. All three
   sample formats now parse (`23/02239/OUT`, `24/05892/FULL`,
   `25/03133/FULL`, `2025/1231`); junk guards unchanged — `_plausible_ref`
   still requires 6+ chars, a digit and a `/`, and rejects bare dates, so
   e.g. "drawing ref 12/05/2023" still yields nothing.
3. **`role_of` had building-services false positives** hiding exactly the
   standalone mains this note flags: "air condition unit" matched the
   `\bcondition\b` discharge keyword, and "discharge flue" / "discharge of
   extracted air" matched `discharge` — so the three examples above all
   classified as `discharge`. `role_of` now strips those phrases before
   keyword matching. With the fix, the current queue yields 6 originals
   (the 3 examples + 3 more); all 6 had already been grouped into standalone
   1-app projects by the worker, so the sweep refreshes their stale stored
   `role` in place (never re-`group()`s a member — that could relocate it
   into an unrelated address bucket), re-derives state (provisional →
   clean), and resolves the review rows.
