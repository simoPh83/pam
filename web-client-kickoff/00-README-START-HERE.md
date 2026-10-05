# PAM web client — kickoff brief for an AI coding agent

Paste this file first, then `01-database-contract.md`, then `02-ui-spec-and-setup.md`,
then `03-project-grouping-ui.md` (how to read grouped projects), and
`04-frontend-spec-new-features.md` (practices, stars, alerts, project search).
`2026.10.02-web-app-roadmap.md` and `2026.09.29-next-steps-roadmap.md` are background
(from the fetcher repo); the three numbered files take precedence where they differ.

## What PAM is
PAM finds London planning applications that are good leads for an **architecture
photographer** (approved schemes that will soon be built/completed → offer photography
to the architect/agent). A separate Python repo (`simoPh83/pam`) runs a worker on
Railway that pulls data from the PlanIt API into **Supabase Postgres** (~138k
applications across London boroughs, growing). **This new repo is the web UI only.**
The worker repo is not touched from here.

## Your job
Build a multi-user (invite-only) web app (Next.js, App Router, TypeScript, deployed on Vercel) that:
1. Lists/filters/sorts leads from the fetcher tables (read-only).
2. Shows lead detail (application data, state history, links).
3. Lets the user log outreach and notes (new tables that this app owns).
4. Later: group leads by practice/agent, enrichment, scoring (see roadmap Phases 4–5).

Start with a thin vertical slice: auth → leads table with filters → lead detail →
outreach log. Don't over-engineer.

## Stack (decided)
- Next.js (App Router) + TypeScript, Tailwind + shadcn/ui, TanStack Table.
- Supabase: `@supabase/ssr` + `@supabase/supabase-js`. Auth: Supabase Auth email sign-in only (no sign-up page; users are invited from the Supabase dashboard; public signups disabled).
- Deploy: Vercel, env vars in Vercel. Never commit secrets (`.env.local` is gitignored).
- Package manager: pnpm or npm (pick one).

## Hard rules
- **The fetcher owns its tables; the UI never writes to them.** Fetcher tables:
  `applications`, `state_history`, `fetch_progress`, `authority_urls`, `jobs`, and the
  view `applications_full`. UI-owned tables (outreach, notes, practices…) live
  separately, e.g. in the same DB under a clear naming (`ui_*`) or the `app` schema,
  so the fetcher can be wiped and re-run without losing human data. Link by `uid`
  (soft reference, no FK that cascades deletes).
- **Security:** RLS is ALREADY applied to the fetcher tables (`scripts/rls.sql` in the worker repo): `anon` has no access, any `authenticated` user has SELECT only. Do not weaken it. UI-owned tables get RLS too, with three tiers (final model, 2026-10-05): **shared** — practices and project links (everyone reads/writes); **read-shared, write-own** — stars (everyone sees who starred what, you can only write your own); **private** — outreach log and alerts (each user sees only their own rows; RLS `using (auth.uid() = created_by)`). Always store `created_by = auth.uid()`.
- Prefer querying through the server (Server Components / Route Handlers) with the
  user's session; never expose the service-role key to the browser.
- Use the **transaction pooler** (port 6543) if connecting with a raw Postgres driver
  from Vercel serverless; supabase-js talks over HTTPS and needs no pooler.
- Supabase free tier = 500 MB. Avoid heavy denormalised copies; use views and indexes.
- Add indexes via migrations when filters need them (check `EXPLAIN`); the table has
  ~140k rows and will reach ~300k+.
- Keep SQL migrations in `supabase/migrations/` in this repo.

## Deliverables for the first session
1. Scaffolded project, lint/typecheck passing, README with setup steps.
2. `.env.example` (see 02 file), Supabase client helpers, auth (magic link) + middleware
   protecting all routes.
3. SQL migration: UI-table RLS + indexes + a `leads` view (see 01 file).
4. Leads page with server-side pagination/filtering/sorting.
5. Lead detail page.
6. Outreach table + simple log UI on the lead detail page.
Ask the user for anything missing (Supabase URL/keys, allowed email) rather than guessing.
