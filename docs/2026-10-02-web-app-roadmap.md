# Web application roadmap — server fetcher + Supabase + Vercel UI

**Date:** 2026-10-02
**Goal:** move fetching off the laptop (days-long backfills, scheduled
incremental sync) and add a UI to filter leads, log outreach and enrich leads
with practice/contact info.

## Architecture

```
PlanIt API --> Railway worker (Python, cron + backfill job) --> Supabase Postgres
                                                                   ^
                                       Vercel (Next.js UI + Supabase Auth)
```

- **Worker** owns the fetcher tables; **UI** owns the human tables. Never mix
  writes: the fetcher must be able to be wiped/re-run without losing notes.
- PlanIt rate limits are per IP: run **one** fetch process at a time.

## Phases

### Phase 0 — Accounts and setup (user, in parallel)
- Supabase project (region: London/eu-west-2). Save the **direct** connection
  string and the **pooler** (transaction) string; use the pooler for the UI,
  direct/session for the worker.
- Railway project with one service (worker) from this GitHub repo; add a
  Postgres-free setup (DB is Supabase). Set `DATABASE_URL` as a Railway variable.
- Never commit secrets; `.env` locally, Railway/Vercel variables remotely.

### Phase 1 — Postgres backend for the fetcher
- Abstract `Ledger` behind a DB layer (SQLite for local dev, Postgres via
  `psycopg` in prod). Port schema: `applications`, `fetch_progress`,
  `state_history`; add `boroughs` table (replaces duplicated authority text).
- Migration script: SQLite -> Postgres (one-off, ~46k rows, 63 MB).
- Replace the xlsx leads output with a `leads` view/query (xlsx stays optional).
- Add `jobs`/`runs` table: start, end, mode, area, counts, errors.

### Phase 2 — Server runtime
- Dockerfile + `python -m pam.worker` entrypoint.
- Job queue in DB: backfill jobs (area, from, to, all states) processed
  sequentially, resumable via `fetch_progress` (survives redeploys).
- Railway cron: daily `--since` sync for all enabled boroughs.
- Massive refetch queue: all-states backfill for the 5 complete boroughs, then
  the other 3-month boroughs extended to 3 years, then remaining London boroughs.
- Alerts: failed run / stale source (no new data N days) notification.

### Phase 3 — UI v1 (Next.js on Vercel, Supabase Auth, invite-only multi-user)
- Leads table: filter by borough, state, decision, date ranges, keywords,
  app type; sort; saved filters.
- Lead detail: application data, state history, links to PlanIt/council.
- Outreach log (human table): `outreach(lead/project, date, channel, status,
  notes)`; statuses e.g. contacted / replied / follow-up due.
- Row-level security: DONE for fetcher tables (scripts/rls.sql: authenticated = read-only, anon = none). UI tables: authenticated = full access.

### Phase 4 — Enrichment
- `practices` (name, website, email, notes) and `contacts`; link agents
  -> practices. Group leads by practice (one email per practice).
- Manual entry first; consider assisted lookup later.

### Phase 5 — Scoring and linking
- Project linking (normalised address + parent-reference in description).
- Completion-window scoring and lanes (per `2026.09.29-next-steps-roadmap.md`)
  as SQL views.

## Decisions to make
- Keep xlsx/Google Sheet output once the UI exists? (suggest: drop.)
- UI auth: Supabase Auth email sign-in, invite-only (no sign-up page), multiple users; outreach shared between users (store created_by).
- Storage: Supabase free tier is 500 MB; ~1.4 KB/row -> ~275 MB for 200k rows.
  Fine, but trim URL columns (derivable) before going to all London.

## Risks
- Railway egress IP may differ in rate-limit behaviour from the laptop: test
  with one small borough first.
- Free-tier Supabase pauses after inactivity; the daily cron keeps it active.
- Offset paging drift: mitigated by weekly deeper `--since` run.
