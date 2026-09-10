-- =====================================================================
-- Local test shim for the parts of Supabase that live outside our
-- migrations: the `auth` schema, auth.uid(), and the anon/authenticated/
-- service_role roles. Supabase provides these in a real project; a bare
-- PostgreSQL instance does not, so the migrations cannot be applied (and
-- therefore cannot be tested) without them.
--
-- Used only by the local/CI test database. Never applied to Supabase.
-- =====================================================================
create schema if not exists auth;

-- Supabase resolves the caller from the `request.jwt.claims` GUC that
-- PostgREST sets per request. Same contract here.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(
    coalesce(
      current_setting('request.jwt.claim.sub', true),
      (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
    ), ''
  )::uuid;
$$;

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end $$;

grant usage on schema public, auth to anon, authenticated, service_role;
grant execute on all functions in schema auth to anon, authenticated, service_role;

-- Supabase grants table privileges to these roles out of the box; a bare
-- PostgreSQL does not, and without them every policy check is unreachable
-- behind a plain "permission denied". Applied as default privileges so they
-- cover the tables the migrations are about to create.
--
-- Note the asymmetry, which mirrors Supabase: `service_role` gets full DML and
-- BYPASSRLS, `authenticated` gets DML that RLS then constrains, and `anon` gets
-- read access that RLS reduces to nothing. Migrations later REVOKE delete on
-- the clinical tables; those revokes must run after this and they do.
alter default privileges in schema public
  grant select, insert, update, delete on tables to authenticated, service_role;
alter default privileges in schema public
  grant select on tables to anon;
alter default privileges in schema public
  grant usage, select on sequences to authenticated, service_role;
alter default privileges in schema public
  grant execute on functions to authenticated, service_role;
