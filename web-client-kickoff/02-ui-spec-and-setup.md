# UI spec v1 and setup

## Env vars (`.env.example`)
```
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
# SUPABASE_SERVICE_ROLE_KEY=   # only if really needed, server-only
```

## Pages
1. `/login` — email sign-in only (no sign-up page). Users are invited from the Supabase dashboard; signups disabled. First test user: info@simonemorciano.com.
2. `/` Leads table (default view = the `leads` view)
   - Filters: borough (multi), state (Permitted/Conditions/…), app size (Medium/Large
     default on), decided date range (default last 90 days), app type, agent/company
     text, free-text on description/address/postcode, "has agent" toggle,
     "not yet contacted" toggle, starred.
   - Sort: decided date (default desc), borough, size.
   - Server-side pagination (50/page), URL-synced filters, saved filter presets
     (store in a `ui_saved_filters` table).
   - Row shows: reference, borough, address, short description, size, state,
     decided date, agent_display, outreach status badge, star.
3. `/lead/[uid]` — Detail
   - All key application fields, description in full, map link (lat/lng), links to
     council page / documents / PlanIt (from `applications_full`).
   - State history timeline from `state_history`.
   - Agent block (company, person, address) with "other leads by this agent" list.
   - Outreach log (shared by all users; show who logged each entry): add entry (channel, to, status, follow-up date, notes), list of
     previous entries, notes/star/hide.
4. `/outreach` — all outreach entries, filter by status, "follow-ups due" first.
5. `/status` (admin) — data freshness: per borough last successful sync, row counts,
   job queue summary from `jobs`.

## UX principles
Dense, fast, keyboard-friendly table; desktop first, usable on mobile. No marketing
fluff. Dark/light follows system.

## Build order
1. Scaffold + auth + middleware.
2. Migration 001: indexes, `leads` view (fetcher-table RLS already applied).
3. Leads table + filters.
4. Detail page + `state_history`.
5. Migration 002: `ui_outreach`, `ui_lead_meta`, RLS; outreach UI.
6. `/outreach`, `/status`.
7. Deploy to Vercel; set env vars; test magic-link redirect URLs (Supabase → Auth →
   URL configuration: add the Vercel domain and `http://localhost:3000`).

## Later phases (don't build yet)
Practice grouping/enrichment (one email per practice), project linking
(normalised address + parent reference), completion-window scoring/lanes as SQL
views, CSV export, email templates, reminders.

## Testing / verification
- Typecheck + lint clean; Playwright smoke test for login redirect and the leads
  table; confirm with the anon key (not the service key) that unauthenticated
  requests return nothing (RLS works).
- Check query plans for the main list query stay < 200 ms at ~300k rows.
