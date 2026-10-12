# Manual resolution of unlinked applications — UI spec for pam-client

**Date:** 2026-10-12 · **For:** `simoPh83/pam-client` · **Backend:** `simoPh83/pam`
**Amends:** `2026-10-10-grouping-review-ui-spec.md` Scenario 3 (which shipped
as "no actions, optional dismiss") and extends Scenario 2's assign with a
wider candidate search.
**Backend ships in:** `scripts/apply_manual_resolution_rpc.py` (new RPCs),
`scripts/apply_grouping_rpc.py` (unique-index-safe assign fix),
`pam/projects.py` (`manual_parent_refs`, `role_override`).

## Purpose

Let the user close `unlinked_no_scheme` / `unlinked_multi_scheme` review rows
by hand when the parser can't: paste the parent reference from the
application description, or — when no reference exists — pick a scheme at the
postcode, declare the application a standalone scheme ("singleton"), or
dismiss it.

Parser improvements remain the *batch* path (see
`2026-10-12-queue-case-review-playbook.md`); this UI is the long tail.
Refs pasted here are recorded (`project_applications.parent_ref` where
`linked_by='manual'`, plus `manual_parent_refs`) and can be mined later for
new parser formats.

## Backend contract

All writes go through SECURITY DEFINER RPCs (authenticated only; the review
row must still be `pending` or the call errors). The UI never writes
`grouping_review`, `project_applications`, `missing_parents` or
`manual_parent_refs` directly.

### `resolve_review_with_ref(p_review_id bigint, p_reference text) → jsonb`

The user pastes the parent ref the parser missed. The server normalises it
(trim, collapse whitespace, drop spaces around `/`, upper-case) and rejects
implausible strings (`400` via raised exception: must match
`^[A-Z0-9][A-Z0-9./-]{2,49}$` and contain a digit — mirror this client-side
for instant feedback).

The ref is **always** recorded in `manual_parent_refs` (one row per
application; a re-paste on a still-pending row replaces it). Then:

| Outcome `action` | When | Effect | UI |
|---|---|---|---|
| `linked` | parent found in `applications` **and** grouped | the application is moved into the parent's project (`linked_by='manual'`, `parent_ref=<ref>`), aggregates refreshed immediately, root/state re-derived by the worker, review row `resolved` | Success toast; replace the queue row with a link to the project (`project_id` in the payload) |
| `queued_for_hunt` | parent not stored (or stored but not yet grouped) | ref catalogued in `missing_parents` (`requested_by=<uid>`), review row `resolved` with `resolution='queued_for_hunt'` | Info: *"Reference saved — the next parent hunt will fetch it and link automatically."* Row leaves the queue |
| `exhausted` | this ref was already hunted and definitively missed on PlanIt | nothing else changes; **review row stays `pending`** | Warning: *"PlanIt has no record of this reference (already looked up). Check the ref, or choose another option."* Keep the dialog open |

Payload extras: `uid`, `reference`, plus `parent_uid`/`project_id` for
`linked`, `ref_year` for `queued_for_hunt`.

Lifecycle after `queued_for_hunt`: the next parent hunt
(`python -m pam.worker enqueue-parent-hunt`) looks the ref up (local-first,
then PlanIt `id_match`); on a hit the child is regrouped and the **recorded
manual ref** links it into the parent's scheme — no further UI action. On a
miss the row goes `exhausted` and the application stays a provisional
singleton (same as parser-found refs that don't resolve).

### `resolve_review_singleton(p_review_id bigint) → jsonb`

The user declares the application a standalone scheme (its own origin, no
parent). Sets `role='original'` + `role_override='original'` and
`linked_by='manual'`; if the application shares a provisional bucket with
other records it is first moved to its own project. Returns
`{action: 'singleton', uid, project_id}`. The review row is `resolved` with
`resolution='singleton'`.

**Durability guarantees (worker-side):** `group()` uses `role_override`
verbatim (never re-derives it) and never relocates an overridden application
into an address bucket — only a *resolved reference* can move it later. The
`promote_standalone_originals.py` sweep skips overridden rows.

### Existing RPCs reused unchanged

- **Assign to a scheme:** `apply_grouping_resolution(p_review_id,
  p_resolution := '<project_id>')` — the assign path now also moves the
  application out of its previous project (the unique index
  `project_applications_uid_key` makes the old upsert-only path fail) and
  queues abandoned projects for re-derivation.
- **Dismiss:** `apply_grouping_resolution(p_review_id, 'dismiss')`.

## The dialog

Entry point: the greyed *"No linked scheme"* row (Scenario 3) and the
*"1 unlinked application"* banner (Scenario 2) get a **Resolve manually**
action opening a dialog with two modes.

### Mode 1 — "Paste a reference" (default)

- Show the application's full description (scrollable) so the user can copy
  the ref out of it; a text input for the ref.
- On submit → `resolve_review_with_ref`; render per the outcome table above.
- Client-side pre-validation: the regex above; also strip a leading
  "ref:"/"ref no." label if the user pastes it.

### Mode 2 — "No reference available"

A two-scope candidate browser plus the fallback actions:

1. **Same address** (default): schemes at the row's `(authority,
   address_key)` — for `unlinked_multi_scheme` this is exactly the
   `candidates` jsonb already on the row; for `unlinked_no_scheme` query
   `projects` by `authority` + `address_key` (usually empty — that's why the
   row exists).
2. **Same postcode** (expand): applications sharing the postcode — catches
   schemes filed under a different address form (e.g. Southwark "Street
   Record …"). Query `applications` with a normalised postcode match
   (`upper(replace(postcode, ' ', ''))` = same normalisation the worker's
   `address_key` uses), joined to their project membership; group rows by
   `project_id` so the user picks a **scheme**, not an application. Show:
   project id, address, latest description, app_type of root, start/decision
   dates, member count. Paginate (a busy postcode can have hundreds).

Actions per candidate: **Assign to this scheme** → `apply_grouping_resolution`
with the project id (existing Scenario 2 behaviour).
Dialog-level fallbacks: **Make it a standalone scheme** →
`resolve_review_singleton`; **Dismiss** → `apply_grouping_resolution(...,
'dismiss')`.

## Edge cases & states

- **Row no longer pending** (worker resolved it meanwhile): the RPCs raise
  `review row <id> is not pending` — refetch the queue and show a toast.
- **Double-submit / two tabs:** the row is locked `FOR UPDATE`; second call
  errors as above. Disable the submit button while in flight.
- **`exhausted` refs:** the review row deliberately stays pending so the user
  can reassess — the dialog must stay open on that outcome.
- **Re-pasting** a different ref is possible while the row is pending
  (replaces the `manual_parent_refs` row); once resolved (`linked`,
  `queued_for_hunt`, `singleton`), the row is closed and the RPCs refuse.
- **Singleton on a shared bucket:** the application is silently moved to its
  own project first — no extra UI needed, but the returned `project_id` is
  the *new* project; navigate to it.
- **Visibility of manual state:** `manual_parent_refs` is readable by
  authenticated users — a lead detail page may show *"Parent ref
  25/AP/0349 added manually"* when a row exists for the uid.

## Deploy order (worker repo)

1. Stop the worker.
2. `python scripts/apply_grouping_rpc.py --apply` (assign-path fix).
3. `python scripts/apply_manual_resolution_rpc.py --apply` (new RPCs;
   also creates `manual_parent_refs` + `role_override` idempotently).
4. Deploy the `pam` code change and restart the worker (running processes
   keep the old `group()`; see the playbook's module-snapshot gotcha).

pam-client can ship any time after step 3; steps 2–4 are what make the links
*durable* across regroups.

## Out of scope

- Bulk paste / multi-row resolution.
- Re-arming `exhausted` refs from the UI (needs a worker decision on retry
  policy; today they're definitive).
- Undoing a `singleton` or `linked` resolution from the UI (doable in SQL;
  revisit if it turns out to be common).
