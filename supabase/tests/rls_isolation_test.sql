-- =====================================================================
-- RLS Tenant-Isolation Proof  —  run in Supabase Dashboard → SQL Editor
-- Demonstrates that a user in Clinic A can see ONLY Clinic A's patients,
-- and a user in Clinic B sees ONLY Clinic B's — enforced by the database,
-- not the application. Everything is rolled back at the end (no changes kept).
-- =====================================================================
begin;

-- Temp users mapping fake auth IDs to the two seeded demo clinics.
insert into public.users (id, clinic_id, auth_uid, name) values
  ('99999999-0000-0000-0000-00000000000a',
   '11111111-1111-1111-1111-111111111111',
   '00000000-0000-0000-0000-0000000000aa', 'Dr A'),
  ('99999999-0000-0000-0000-00000000000b',
   '22222222-2222-2222-2222-222222222222',
   '00000000-0000-0000-0000-0000000000bb', 'Dr B')
on conflict (id) do nothing;

-- ---- Impersonate the Clinic A doctor ----
set local role authenticated;
select set_config('request.jwt.claims',
       '{"sub":"00000000-0000-0000-0000-0000000000aa","role":"authenticated"}', true);
select 'Clinic A doctor sees:' as context, name from public.patients;
-- EXPECT: only "Asha Rao (Clinic A)"

-- ---- Impersonate the Clinic B doctor ----
reset role;
set local role authenticated;
select set_config('request.jwt.claims',
       '{"sub":"00000000-0000-0000-0000-0000000000bb","role":"authenticated"}', true);
select 'Clinic B doctor sees:' as context, name from public.patients;
-- EXPECT: only "Bilal Khan (Clinic B)"

reset role;
rollback;  -- discard the temp users; nothing is persisted
