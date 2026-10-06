-- RLS for fetcher tables. Run once (idempotent). The worker connects as the
-- postgres role, which bypasses RLS, so it is unaffected.
-- Model: any signed-in (authenticated) user can READ; nobody but the worker writes;
-- anon (not signed in) gets nothing.

do $$
declare t text;
begin
  foreach t in array array['applications','state_history','fetch_progress',
                           'authority_urls','jobs']
  loop
    if to_regclass('public.'||t) is not null then
      execute format('alter table public.%I enable row level security', t);
      execute format('revoke all on public.%I from anon', t);
      execute format('revoke insert, update, delete, truncate on public.%I from authenticated', t);
      execute format('grant select on public.%I to authenticated', t);
      execute format('drop policy if exists "authenticated read" on public.%I', t);
      execute format('create policy "authenticated read" on public.%I for select to authenticated using (true)', t);
    end if;
  end loop;
end $$;

-- Views run with the caller's rights so RLS on the base tables applies.
do $$
begin
  if to_regclass('public.applications_full') is not null then
    alter view public.applications_full set (security_invoker = true);
    revoke all on public.applications_full from anon;
    grant select on public.applications_full to authenticated;
  end if;
end $$;


-- projects / project_applications (2026-10-04)
alter table projects enable row level security;
alter table project_applications enable row level security;
revoke all on projects from anon;
revoke all on project_applications from anon;
revoke insert, update, delete on projects from authenticated;
grant select on projects to authenticated;
revoke insert, update, delete on project_applications from authenticated;
grant select on project_applications to authenticated;
drop policy if exists "authenticated read" on projects;
create policy "authenticated read" on projects for select to authenticated using (true);
drop policy if exists "authenticated read" on project_applications;
create policy "authenticated read" on project_applications for select to authenticated using (true);


-- missing_parents (2026-10-05): worker-owned, users read-only
alter table missing_parents enable row level security;
revoke all on missing_parents from anon;
grant select on missing_parents to authenticated;
drop policy if exists "authenticated read" on missing_parents;
create policy "authenticated read" on missing_parents for select to authenticated using (true);
