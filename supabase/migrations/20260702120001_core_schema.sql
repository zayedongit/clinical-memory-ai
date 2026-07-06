-- =====================================================================
-- Clinical Memory AI — Core schema (Phase 0)
-- Tables: clinics, users, patients, visits, clinical_facts, audit_log
-- + current_clinic_id() helper used by all Row-Level Security policies.
-- Append-only clinical_facts; every table is clinic-scoped for multi-tenancy.
-- =====================================================================

-- gen_random_uuid() lives in pgcrypto (present on Supabase, safe to ensure)
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------
-- clinics : tenant root
-- ---------------------------------------------------------------------
create table if not exists public.clinics (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_at  timestamptz not null default now()
);

-- ---------------------------------------------------------------------
-- users : maps a Supabase Auth user (auth_uid) to a clinic
-- ---------------------------------------------------------------------
create table if not exists public.users (
  id          uuid primary key default gen_random_uuid(),
  clinic_id   uuid not null references public.clinics(id) on delete cascade,
  auth_uid    uuid not null unique,            -- equals auth.uid() from the JWT
  name        text not null,
  role        text not null default 'doctor' check (role in ('doctor','staff')),
  created_at  timestamptz not null default now()
);
create index if not exists users_clinic_id_idx on public.users(clinic_id);
create index if not exists users_auth_uid_idx  on public.users(auth_uid);

-- ---------------------------------------------------------------------
-- Helper: resolve the caller's clinic from their JWT.
-- SECURITY DEFINER so it can read public.users even under RLS; it only
-- ever returns the caller's OWN clinic, so this is safe.
-- ---------------------------------------------------------------------
create or replace function public.current_clinic_id()
returns uuid
language sql
stable
security definer
set search_path = public
as $$
  select clinic_id
  from public.users
  where auth_uid = auth.uid()
  limit 1;
$$;

-- ---------------------------------------------------------------------
-- patients : clinic-scoped; soft-match + merge support
-- ---------------------------------------------------------------------
create table if not exists public.patients (
  id           uuid primary key default gen_random_uuid(),
  clinic_id    uuid not null references public.clinics(id) on delete cascade,
  name         text not null,
  dob          date,
  gender       text,
  phone        text,
  match_key    text,                            -- normalized name+phone+dob for dedup
  merged_into  uuid references public.patients(id),
  created_at   timestamptz not null default now()
);
create index if not exists patients_clinic_id_idx on public.patients(clinic_id);
create index if not exists patients_match_key_idx  on public.patients(match_key);

-- ---------------------------------------------------------------------
-- visits : one per consultation
-- ---------------------------------------------------------------------
create table if not exists public.visits (
  id          uuid primary key default gen_random_uuid(),
  patient_id  uuid not null references public.patients(id) on delete cascade,
  clinic_id   uuid not null references public.clinics(id) on delete cascade,
  doctor_id   uuid references public.users(id),
  status      text not null default 'draft' check (status in ('draft','approved')),
  started_at  timestamptz not null default now(),
  approved_at timestamptz
);
create index if not exists visits_patient_id_idx on public.visits(patient_id);
create index if not exists visits_clinic_id_idx  on public.visits(clinic_id);

-- ---------------------------------------------------------------------
-- clinical_facts : APPEND-ONLY provenance core.
-- AI facts enter as 'proposed'; only the doctor review gate promotes to
-- 'confirmed'. Corrections insert a new row referencing supersedes_id.
-- ---------------------------------------------------------------------
create table if not exists public.clinical_facts (
  id            uuid primary key default gen_random_uuid(),
  patient_id    uuid not null references public.patients(id) on delete cascade,
  clinic_id     uuid not null references public.clinics(id) on delete cascade,
  visit_id      uuid references public.visits(id) on delete set null,
  fact_type     text not null check (fact_type in
                  ('diagnosis','medication','allergy','lab_result','vital','symptom','follow_up')),
  value         text not null,
  structured    jsonb not null default '{}'::jsonb,
  source        text not null check (source in
                  ('ai_extracted','doctor_entered','doctor_confirmed_ai')),
  status        text not null default 'proposed' check (status in
                  ('proposed','confirmed','superseded','rejected')),
  confidence    numeric,
  asserted_by   uuid references public.users(id),
  asserted_at   timestamptz not null default now(),
  supersedes_id uuid references public.clinical_facts(id)
);
create index if not exists clinical_facts_patient_idx on public.clinical_facts(patient_id);
create index if not exists clinical_facts_clinic_idx  on public.clinical_facts(clinic_id);
create index if not exists clinical_facts_lookup_idx   on public.clinical_facts(patient_id, fact_type, status);

-- ---------------------------------------------------------------------
-- audit_log : immutable record of state changes
-- ---------------------------------------------------------------------
create table if not exists public.audit_log (
  id         uuid primary key default gen_random_uuid(),
  clinic_id  uuid references public.clinics(id) on delete set null,
  actor_id   uuid references public.users(id),
  action     text not null,
  entity     text not null,
  entity_id  uuid,
  before     jsonb,
  after      jsonb,
  at         timestamptz not null default now()
);
create index if not exists audit_log_clinic_idx on public.audit_log(clinic_id);
