# PlanIt update logic — how records change over time

**Date:** 2026-10-01 (updated 2026-10-02)
**Question:** can we assume applications never update in place (a new entry appears
later referencing the old one), making historical re-fetches unnecessary?
**Answer:** **No.** Records mutate in place; a rolling window on registration
date misses most state transitions.

## Evidence

### Records mutate in place (2026-10-01)
- Across 15,914 rows, only 16 duplicated `(authority, reference)` pairs, none a
  lifecycle update (plain ref + sub-type ref double-listing).
- One PlanIt record (`name` = uid) mutates `app_state`, `decided_date`,
  `decision`, `n_documents`, `n_comments`, `status`, `description`.

### Enfield dry re-fetch (2026-10-02, 3-4 days after first fetch)
- 818 shared records, **52 changed state**: 25 Undecided->Rejected,
  18 ->Permitted, 9 ->Conditions. Also changes in documents, comments, status,
  and 9 descriptions.
- **Rejected / Withdrawn were never fetched** (only Permitted, Conditions,
  Undecided were requested), so those rows went stale as Undecided.
- Offset paging over a live dataset drifted: ~15 records duplicated and ~15
  dropped within one run.

### PlanIt change metadata
- Every record carries `last_changed`, `last_different`, `last_scraped`
  (previously not stored; now columns in `applications`).
- `different_start=YYYY-MM-DD` filters server-side on `last_different` —
  Enfield returned 337 records changed since 10-01. This is the right
  incremental key.
- `recent=` returns nothing useful. A query with no spatial/date/search
  restriction gets HTTP 400.
- `start_date`/`end_date` filter *registration* date, not decision date.

### Project grouping (complete 3-year boroughs)
| Borough | Records sharing an address with another |
|---|---|
| Westminster | 80% |
| City | 84% |
| Camden | 55% |
| Islington | 50% |
| Hackney | 49% |

- Only Islington has `uprn` (100%); UPRN is unreliable elsewhere, so use
  normalised address.
- Many descriptions cite a parent reference (discharge of conditions, NMA,
  variation); the parent exists in our DB for ~40-60% of those.
- Amendments/resubmissions arrive as new records with new references; PlanIt
  has no "supersedes" field.

### Source availability
- PlanIt's news page lists stale/unavailable authorities. **Hackney** is on it:
  data stops around May 2026.

## What is implemented

- `fetch_all_states: true` (config): no `app_state` filter, so
  Rejected/Withdrawn/etc. come back.
- `--since DATE`: incremental mode using `different_start`; resume window key
  `since_{date}`. A completed `--since` date is skipped on rerun, so scheduled
  runs must use a new date each time (overlap ~1 day).
- `--from` now requires `--to` (the resume key is `{from}_{to}`; a moving end
  date previously restarted a backfill from offset 0).
- `last_changed`, `last_different`, `last_scraped` stored per record.
- `state_history` table: a row on first sighting and on each `app_state`
  change (old/new state, decided_date, decision, last_different). Seeded
  from existing rows (`old_state` NULL, `observed_at = first_seen`).
- `other_fields_json` holds only extras not parsed into columns
  (DB 136 MB -> 63 MB).
- Dry runs no longer write `fetch_progress`.
- First incremental run: Camden `--since 2026-10-01` -> 38 records, 2
  Undecided->Permitted transitions logged.

## Recommended run strategy

- **Backfill** per borough with all states and a fixed `--from/--to`.
- **Daily (cron):** `--since <yesterday - 1 day>` for every enabled borough.
  Catches new registrations, decisions and edits in one query.
- **Weekly:** a deeper `--since` (e.g. 30 days) to absorb paging drift.
- Existing complete backfills lack Rejected/Withdrawn history; re-run them with
  all states (on the server).

## Open items

- Parent-reference and address-based project linking (v2).
- Rate limiting dominates long runs: ~1h per borough for ~6k records, ~85% in
  429 Retry-After waits.
