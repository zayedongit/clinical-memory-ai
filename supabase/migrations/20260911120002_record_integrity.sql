-- =====================================================================
-- Clinical record integrity.
--
--   A. Optimistic locking on visits, so two physicians finalising the same
--      consultation cannot silently overwrite each other.
--   B. Attested notes become immutable content. Corrections are recorded
--      as appended amendments; the signed text is never rewritten.
--   C. clinical_facts is genuinely append-only: values are frozen at
--      insert, and only legal status transitions are permitted.
--   D. clinical_status on each fact, so longitudinal memory can tell a
--      CURRENT problem from a RESOLVED one instead of treating every
--      historical fact as present-tense.
--
-- Enforced by triggers, which fire for every role including service_role.
-- Additive + idempotent.
-- =====================================================================

-- ---------------------------------------------------------------------
-- A. Optimistic locking
-- ---------------------------------------------------------------------
alter table public.visits add column if not exists version integer not null default 1;

create or replace function public.visits_bump_version()
returns trigger
language plpgsql
as $$
begin
  -- Any content change advances the version. A finaliser that read version N
  -- and writes with expected_version N will be rejected if anyone else wrote
  -- in between (see finalize_visit).
  new.version := coalesce(old.version, 1) + 1;
  return new;
end;
$$;

drop trigger if exists visits_version_t on public.visits;
create trigger visits_version_t
  before update on public.visits
  for each row execute function public.visits_bump_version();

-- ---------------------------------------------------------------------
-- B. Attested notes are content-immutable; corrections append.
-- ---------------------------------------------------------------------
alter table public.soap_notes
  add column if not exists amendments jsonb not null default '[]'::jsonb;

create or replace function public.soap_notes_attested_immutable()
returns trigger
language plpgsql
as $$
begin
  if coalesce(old.attested, false) is not true then
    return new;                                   -- drafts stay freely editable
  end if;

  -- An attested note may only be soft-deleted or amended. Every clinical
  -- column is frozen: the physician signed this exact text.
  if (new.transcript              is distinct from old.transcript)
  or (new.dialogue                is distinct from old.dialogue)
  or (new.subjective              is distinct from old.subjective)
  or (new.objective               is distinct from old.objective)
  or (new.assessment              is distinct from old.assessment)
  or (new.plan                    is distinct from old.plan)
  or (new.entities                is distinct from old.entities)
  or (new.prescription            is distinct from old.prescription)
  or (new.clinical_considerations is distinct from old.clinical_considerations)
  or (new.vitals                  is distinct from old.vitals)
  or (new.sign_off                is distinct from old.sign_off)
  or (new.attested_at             is distinct from old.attested_at)
  or (new.attested_by             is distinct from old.attested_by)
  or (new.attested                is distinct from old.attested)
  then
    raise exception
      'soap_notes %: attested clinical content is immutable. Record a correction as an amendment.',
      old.id
      using errcode = 'check_violation';
  end if;

  return new;
end;
$$;

drop trigger if exists soap_notes_attested_immutable_t on public.soap_notes;
create trigger soap_notes_attested_immutable_t
  before update on public.soap_notes
  for each row execute function public.soap_notes_attested_immutable();

-- ---------------------------------------------------------------------
-- C. clinical_facts: append-only in fact, not just by convention.
-- ---------------------------------------------------------------------
create or replace function public.clinical_facts_append_only()
returns trigger
language plpgsql
as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'clinical_facts is append-only; DELETE is not permitted'
      using errcode = 'check_violation';
  end if;

  if (new.patient_id  is distinct from old.patient_id)
  or (new.clinic_id   is distinct from old.clinic_id)
  or (new.visit_id    is distinct from old.visit_id)
  or (new.fact_type   is distinct from old.fact_type)
  or (new.value       is distinct from old.value)
  or (new.structured  is distinct from old.structured)
  or (new.source      is distinct from old.source)
  or (new.asserted_by is distinct from old.asserted_by)
  or (new.asserted_at is distinct from old.asserted_at)
  then
    raise exception 'clinical_facts %: asserted facts are immutable; insert a superseding row instead',
      old.id using errcode = 'check_violation';
  end if;

  -- Only the lifecycle may move, and only forwards.
  if new.status is distinct from old.status then
    if not (
         (old.status = 'proposed'  and new.status in ('confirmed', 'rejected', 'superseded'))
      or (old.status = 'confirmed' and new.status = 'superseded')
    ) then
      raise exception 'clinical_facts %: illegal status transition % -> %',
        old.id, old.status, new.status using errcode = 'check_violation';
    end if;
  end if;

  return new;
end;
$$;

drop trigger if exists clinical_facts_append_only_t on public.clinical_facts;
create trigger clinical_facts_append_only_t
  before update or delete on public.clinical_facts
  for each row execute function public.clinical_facts_append_only();

revoke delete on public.clinical_facts from anon, authenticated;

-- ---------------------------------------------------------------------
-- D. Temporal meaning of a fact.
--
--   current     — believed true right now (an active problem, a current med)
--   historical  — true in the past, relevant but not active
--   resolved    — explicitly resolved (the symptom went away)
--   unknown     — recorded without enough information to place it
--
-- Distinct from `status`, which is about provenance/lifecycle
-- (proposed -> confirmed -> superseded). A fact can be confirmed and
-- resolved at the same time; longitudinal memory needs both axes.
-- ---------------------------------------------------------------------
alter table public.clinical_facts
  add column if not exists clinical_status text not null default 'current';

alter table public.clinical_facts drop constraint if exists clinical_facts_clinical_status_check;
alter table public.clinical_facts
  add constraint clinical_facts_clinical_status_check
  check (clinical_status in ('current', 'historical', 'resolved', 'unknown'));

-- When the assertion stopped being current (set when a later visit supersedes it).
alter table public.clinical_facts add column if not exists valid_to timestamptz;

create index if not exists clinical_facts_clinical_status_idx
  on public.clinical_facts(patient_id, fact_type, clinical_status)
  where status = 'confirmed';
