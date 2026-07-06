-- =====================================================================
-- Clinical Memory AI — Demo seed (Phase 0)
-- Two demo clinics with fixed UUIDs so the tenant-isolation test is
-- reproducible. Real users are created at signup and linked via the
-- /clinics/bootstrap endpoint; patients here belong to each clinic.
-- Idempotent: safe to re-run.
-- =====================================================================

insert into public.clinics (id, name) values
  ('11111111-1111-1111-1111-111111111111', 'Demo Clinic A'),
  ('22222222-2222-2222-2222-222222222222', 'Demo Clinic B')
on conflict (id) do nothing;

insert into public.patients (id, clinic_id, name, gender, phone) values
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
   '11111111-1111-1111-1111-111111111111', 'Asha Rao (Clinic A)', 'female', '9000000001'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
   '22222222-2222-2222-2222-222222222222', 'Bilal Khan (Clinic B)', 'male', '9000000002')
on conflict (id) do nothing;
