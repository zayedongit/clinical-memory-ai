-- =====================================================================
-- Visit storage: every consultation's full record, per patient.
--   soap_notes  — transcript, doctor/patient dialogue, SOAP, entities,
--                 and AI follow-up questions (all reviewed by the physician).
-- + patient vitals (height/weight) and a delete policy on visits so a
--   doctor can remove a record.
-- =====================================================================

create table if not exists public.soap_notes (
  id                  uuid primary key default gen_random_uuid(),
  visit_id            uuid not null references public.visits(id) on delete cascade,
  patient_id          uuid not null references public.patients(id) on delete cascade,
  clinic_id           uuid not null references public.clinics(id) on delete cascade,
  transcript          text,
  dialogue            jsonb not null default '[]'::jsonb,   -- [{speaker, text}]
  subjective          text,
  objective           text,
  assessment          text,
  plan                text,
  entities            jsonb not null default '{}'::jsonb,
  follow_up_questions jsonb not null default '[]'::jsonb,   -- [{question, concern, likelihood_pct, severity}]
  created_by          uuid references public.users(id),
  created_at          timestamptz not null default now()
);
create index if not exists soap_notes_patient_idx on public.soap_notes(patient_id);
create index if not exists soap_notes_visit_idx   on public.soap_notes(visit_id);
create index if not exists soap_notes_clinic_idx  on public.soap_notes(clinic_id);

-- Patient vitals collected at first visit (baseline).
alter table public.patients add column if not exists height_cm numeric;
alter table public.patients add column if not exists weight_kg numeric;

-- RLS on soap_notes (clinic-scoped, like every clinical table).
alter table public.soap_notes enable row level security;
drop policy if exists soap_notes_select on public.soap_notes;
drop policy if exists soap_notes_insert on public.soap_notes;
drop policy if exists soap_notes_delete on public.soap_notes;
create policy soap_notes_select on public.soap_notes
  for select using (clinic_id = public.current_clinic_id());
create policy soap_notes_insert on public.soap_notes
  for insert with check (clinic_id = public.current_clinic_id());
create policy soap_notes_delete on public.soap_notes
  for delete using (clinic_id = public.current_clinic_id());

-- Allow a doctor to delete a visit (Phase 0 only granted select/insert/update).
drop policy if exists visits_delete on public.visits;
create policy visits_delete on public.visits
  for delete using (clinic_id = public.current_clinic_id());
