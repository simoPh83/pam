# Grouping review — UI spec for pam-client

**Date:** 2026-10-10 · **For:** `simoPh83/pam-client` · **Backend:** `simoPh83/pam`
**Depends on:** scheme-level grouping (`2026-10-10-scheme-grouping-spec.md`).

## Purpose

Surface grouping ambiguity to the user *in context*, not as a standalone
queue page. Three distinct scenarios, three different presentations.

## Data source: `grouping_review`

| Column | Notes |
|---|---|
| `id` | pk |
| `authority` | |
| `project_id` | **nullable** — set for `multi_root` (the ambiguous project); NULL for orphans |
| `address_key` | set for orphans (`unlinked_*`); join to projects by `(authority, address_key)` |
| `uid` | the orphan application (NULL for `multi_root`) |
| `reason` | `multi_root` · `unlinked_multi_scheme` · `unlinked_no_scheme` |
| `candidates` | jsonb array: `{project_id, uid, description, app_type, start_date, grouping_state, n_applications}` |
| `status` | `pending` (default) · `resolved` · `dismissed` |
| `resolution` | set on resolve: chosen `project_id`, `'new'`, or `'split'` |

RLS: authenticated users can `select` all and `update` only
`status, resolution, resolved_by, resolved_at`. Writes that move data happen
server-side (worker/rpc) — see "Applying a resolution".

---

## Scenario 1 — `multi_root` (209 items)

A project whose reference chain contains **two or more candidate origin
applications** (kept merged). Backed by `projects.grouping_state =
'ambiguous'`.

**Leads list:** normal project row + amber **"Multiple schemes"** badge.

**Project page:** banner — *"This address contains N candidate schemes."*
Lists each candidate root (description, type, filed date) from
`grouping_review.candidates` (join on `project_id`).

**Actions (optional, low-pressure):**
- **Keep as one scheme** → `status='dismissed'`, `resolution='merged'`,
  clear the project flag.
- **Split into separate schemes** → advanced: assign members to each root,
  `resolution='split'`. Default action is *keep merged*; most of these are
  one evolving scheme and are fine left alone.

---

## Scenario 2 — `unlinked_multi_scheme` (6,215 items)

A parentless follow-up (a "details / condition" record) whose address has
**several schemes**. Not a project itself — an orphan pending assignment.

**Leads list:** the orphan does **not** appear as its own row. Instead, each
candidate scheme at that address shows a subtle **"1 unlinked application"**
chip.

**Project page:** banner — *"1 application at this address isn't linked to a
scheme yet."* Shows the orphan's description; candidate schemes from
`candidates`.

**Actions:**
- **Assign to this scheme** → `resolution = <project_id>`; the application
  becomes a member (`linked_by='manual'`).
- **Dismiss** → `status='dismissed'`; the record is dropped (low-value stray
  detail).

**On split:** because the orphan is keyed by `(authority, address_key)`, if
the user splits a multi-root project first, the same orphan surfaces on
**each** resulting project's page (all share the address) until assigned.
This falls out of the address keying for free — no extra work.

---

## Scenario 3 — `unlinked_no_scheme` (2,094 items)

A parentless follow-up at an address with **no scheme to attach to**. Mostly
discharge/details records whose parent predates our data window — the
parent-hunt worker resolves these automatically over time.

**Leads list:** a **greyed single-application row** labelled *"No linked
scheme — parent not yet in our data."* Not a real project (it's a queue row);
render it read-only.

**Actions:** none required. Optional **Dismiss**. When the parent-hunt worker
resolves the parent, the row disappears and the application joins the scheme.

---

## Applying a resolution

The UI calls one RPC — it does not write `grouping_review` directly:

```ts
await supabase.rpc("apply_grouping_resolution", {
  p_review_id: review.id,
  p_resolution: "<project_id>" | "merged" | "dismiss",
});
```

`apply_grouping_resolution(review_id, resolution)` (SECURITY DEFINER,
authenticated-only, validates the row is pending) applies the decision
atomically:

- **assign** (`resolution` = a project id): inserts the orphan into
  `project_applications` (`linked_by='manual'`), refreshes the target
  project's aggregates, queues the project in `regroup_pending`, and marks
  the row `resolved`. The worker's `drain_regroup_pending()` sweep re-derives
  `root_uid`/`grouping_state` on its next run (root/state logic lives in the
  Python worker, not duplicated in SQL).
- **`multi_root` keep merged** (`resolution='merged'`): sets the project's
  `grouping_state='clean'`, marks the row resolved. **Split** is intentionally
  not in the RPC — it's a worker-side operation (member-to-root assignment);
  the UI should surface split as a worker-handled action.
- **dismiss** (`resolution='dismiss'`): marks the row `dismissed`; the orphan
  `applications` row is kept but never grouped (audit history preserved).

## Out of scope for this spec

- Bulk resolution tooling (volume is ~8.5k; revisit if manual triage stalls).
- The `main_uid` headline (separate construction-timing spec, rebased onto
  scheme projects).
