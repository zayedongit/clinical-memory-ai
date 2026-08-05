-- =====================================================================
-- UHID — a human-readable unique patient id (Clinical Synthesis-style CH-YYYY-NNNNNN),
-- auto-generated on insert. Backfills existing patients. Enables searching
-- and referencing patients by a stable clinic id, not just name.
-- =====================================================================
create sequence if not exists public.patient_uhid_seq;
grant usage, select on sequence public.patient_uhid_seq to authenticated;

alter table public.patients add column if not exists uhid text;

create or replace function public.set_patient_uhid()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  if new.uhid is null or new.uhid = '' then
    new.uhid := 'CH-' || to_char(now(), 'YYYY') || '-' ||
                lpad(nextval('public.patient_uhid_seq')::text, 6, '0');
  end if;
  return new;
end $$;

drop trigger if exists patient_uhid_trg on public.patients;
create trigger patient_uhid_trg
  before insert on public.patients
  for each row execute function public.set_patient_uhid();

-- Backfill any existing patients that don't have a UHID yet.
update public.patients
   set uhid = 'CH-' || to_char(coalesce(created_at, now()), 'YYYY') || '-' ||
              lpad(nextval('public.patient_uhid_seq')::text, 6, '0')
 where uhid is null or uhid = '';

create unique index if not exists patients_uhid_uidx on public.patients(uhid);
