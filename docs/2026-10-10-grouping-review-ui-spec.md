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

The UI only writes `status`/`resolution`/`resolved_by`/`resolved_at` on
`grouping_review`. The actual data move is server-side:

- **`unlinked_multi_scheme` assign** → insert into `project_applications`
  (`project_id=resolution`, `linked_by='manual'`), refresh the project's
  aggregates + `grouping_state`.
- **`multi_root` split** → create new project(s), move members, refresh.
- **dismiss (unlinked)** → the orphan application is left out of grouping (or
  deleted, per product call — recommended: keep the `applications` row, just
  never group it; deletion loses audit history).

Recommend a small Postgres function / RPC (e.g. `apply_grouping_resolution
(review_id bigint)`) so the move + refresh is atomic and not duplicated in
client code.

## Out of scope for this spec

- Bulk resolution tooling (volume is ~8.5k; revisit if manual triage stalls).
- The `main_uid` headline (separate construction-timing spec, rebased onto
  scheme projects).
