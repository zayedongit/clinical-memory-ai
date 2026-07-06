-- =====================================================================
-- Clinical Memory AI — Row-Level Security (Phase 0)
-- Default-deny on every clinic-scoped table. A row is visible/writable
-- only when its clinic_id matches the caller's clinic (current_clinic_id()).
-- The Supabase service_role key bypasses RLS (used server-side for audit);
-- the anon key + a user's JWT is fully constrained by these policies.
-- =====================================================================

-- Enable RLS (default-deny once enabled)
alter table public.clinics        enable row level security;
alter table public.users          enable row level security;
alter table public.patients       enable row level security;
alter table public.visits         enable row level security;
alter table public.clinical_facts enable row level security;
alter table public.audit_log      enable row level security;

-- ---------------------------------------------------------------------
-- clinics : caller can see only their own clinic
-- ---------------------------------------------------------------------
create policy clinics_select on public.clinics
  for select using (id = public.current_clinic_id());

-- ---------------------------------------------------------------------
-- users : caller can see users within their clinic
-- ---------------------------------------------------------------------
create policy users_select on public.users
  for select using (clinic_id = public.current_clinic_id());

-- ---------------------------------------------------------------------
-- patients
-- ---------------------------------------------------------------------
create policy patients_select on public.patients
  for select using (clinic_id = public.current_clinic_id());
create policy patients_insert on public.patients
  for insert with check (clinic_id = public.current_clinic_id());
create policy patients_update on public.patients
  for update using (clinic_id = public.current_clinic_id())
             with check (clinic_id = public.current_clinic_id());
create policy patients_delete on public.patients
  for delete using (clinic_id = public.current_clinic_id());

-- ---------------------------------------------------------------------
-- visits
-- ---------------------------------------------------------------------
create policy visits_select on public.visits
  for select using (clinic_id = public.current_clinic_id());
create policy visits_insert on public.visits
  for insert with check (clinic_id = public.current_clinic_id());
create policy visits_update on public.visits
  for update using (clinic_id = public.current_clinic_id())
             with check (clinic_id = public.current_clinic_id());

-- ---------------------------------------------------------------------
-- clinical_facts : append-only. Allow insert + status-only updates;
-- no delete (corrections supersede via new rows).
-- ---------------------------------------------------------------------
create policy clinical_facts_select on public.clinical_facts
  for select using (clinic_id = public.current_clinic_id());
create policy clinical_facts_insert on public.clinical_facts
  for insert with check (clinic_id = public.current_clinic_id());
create policy clinical_facts_update on public.clinical_facts
  for update using (clinic_id = public.current_clinic_id())
             with check (clinic_id = public.current_clinic_id());

-- ---------------------------------------------------------------------
-- audit_log : caller can read their clinic's audit trail.
-- Writes happen server-side via service_role (bypasses RLS), keeping the
-- log tamper-resistant from ordinary user sessions.
-- ---------------------------------------------------------------------
create policy audit_log_select on public.audit_log
  for select using (clinic_id = public.current_clinic_id());
