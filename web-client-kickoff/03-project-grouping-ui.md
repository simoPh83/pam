# Project grouping — UI data spec (pam-client)

**Date:** 2026-10-04
**Scope:** how the web client reads grouped projects instead of flat
applications. Read together with `01-database-contract.md`.

## Concept

One development site = one **project**. All its planning applications
(original permission, s73 variations, condition discharges, NMAs) are members.
Outreach is per project: never email a practice twice about the same site.

Grouping is maintained by the worker (Python, Railway). The client only
reads.

## Tables

### `projects`
| column | type | notes |
|---|---|---|
| `id` | bigint PK | |
| `authority` | text | borough |
| `name` | text | shortest address in the cluster — display name |
| `address_key` | text | normalised address (debug only) |
| `root_uid` | text | uid of the earliest original application |
| `latest_uid` | text | uid of the most recently decided/updated member |
| `n_applications` | int | member count |
| `first_seen`, `last_updated` | date | project-level freshness |

### `project_applications`
`project_id, uid, parent_ref, is_root, role`
(`role`: `original` | `variation` | `discharge` | `nma`). Soft link to
`applications.uid` — always join back to `applications` for display fields.

### `leads` view (preferred read path)
The existing `leads` view now includes **`project_id`** and
**`n_applications`** alongside the usual application columns. Use it instead
of hand-joining.

## Query patterns (Supabase JS)

**Grouped leads list (one row per project):**
```ts
// Fetch leads, then collapse by project_id client-side, or use an RPC.
// Simple approach: fetch the view ordered so the representative row wins.
const { data } = await supabase
  .from("leads")
  .select("*")
  .order("decided_date", { ascending: false });
// group by project_id; representative row = the one whose uid ===
// projects.latest_uid (fetch projects separately) or simply the first
// (most recently decided) row in each group.
```
Show a badge with `n_applications` when > 1.

**Project detail ("other applications on this site"):**
```ts
const { data } = await supabase
  .from("project_applications")
  .select("role, is_root, applications(uid, reference, app_state, decided_date, description)")
  .eq("project_id", projectId)
  .order("is_root", { ascending: false });
```
(If the soft join is not picked up by PostgREST, do two queries: membership
uids from `project_applications`, then `applications` `.in("uid", uids)`.)

**Project header:**
```ts
const { data } = await supabase
  .from("projects")
  .select("*")
  .eq("id", projectId)
  .single();
```

## UI guidance

- **Leads table:** default to collapsed-by-project. Provide a toggle for the
  flat list.
- **Badge:** `n_applications` on grouped rows.
- **Detail page:** section "Other applications on this site" listing role +
  state + decided_date per member, root first.
- **Sorting a project:** use `last_updated` (project-level) for "active"
  ordering; `decided_date` of the representative row for recency.

## Notes / gotchas

- ~2/3 of projects have a single application; `project_id` is never null in
  `leads` (every application is grouped), but code defensively anyway.
- `parent_ref` is debugging metadata — do not display it.
- RLS: both tables are read-only for `authenticated`; no writes from the
  client.
