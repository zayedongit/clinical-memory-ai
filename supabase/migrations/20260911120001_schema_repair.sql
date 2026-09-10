-- =====================================================================
-- Schema repair — three defects that made the committed migrations
-- unable to reproduce the running application.
--
--   1. visits.status CHECK allowed only ('draft','approved'), but the
--      application writes 'in_progress' for drafts and 'approved' on
--      finalize. Every draft save 400s on a database built from these
--      migrations. Widened + normalised.
--
--   2. soap_notes had RLS enabled with SELECT/INSERT/DELETE policies but
--      no UPDATE policy. PostgREST returns 204 for an UPDATE that matches
--      zero rows, so finalising a draft and soft-deleting a note both
--      reported success while writing nothing. Silent clinical data loss.
--
--   3. patients had no soft-delete columns although visits and soap_notes
--      did, so a patient could only ever be hidden via merged_into.
--
-- Additive + idempotent. No data loss.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. visits.status — the states the application actually uses.
--    'draft'       legacy synonym of in_progress (kept so old rows validate)
--    'in_progress' an unfinished consultation the physician can resume
--    'approved'    physician-attested, finalised
--    'completed'   legacy synonym of approved (read paths already treat both)
-- ---------------------------------------------------------------------
alter table public.visits drop constraint if exists visits_status_check;
alter table public.visits
  add constraint visits_status_check
  check (status in ('draft', 'in_progress', 'approved', 'completed'));

-- ---------------------------------------------------------------------
-- 2. soap_notes UPDATE policy — clinic-scoped, like every other write.
--    Content-level immutability of an attested note is enforced by a
--    trigger (see 20260911120002_record_integrity.sql), not by RLS,
--    because RLS cannot express "these columns only".
-- ---------------------------------------------------------------------
drop policy if exists soap_notes_update on public.soap_notes;
create policy soap_notes_update on public.soap_notes
  for update using (clinic_id = public.current_clinic_id())
             with check (clinic_id = public.current_clinic_id());

-- One note per visit. The application already assumes this (it PATCHes by
-- visit_id and reads soap_notes[0]); without the constraint a duplicated
-- insert silently produces two notes and reads become non-deterministic.
create unique index if not exists soap_notes_visit_uidx on public.soap_notes(visit_id);

-- ---------------------------------------------------------------------
-- 3. patients soft-delete, matching visits/soap_notes.
-- ---------------------------------------------------------------------
alter table public.patients add column if not exists deleted_at timestamptz;
alter table public.patients add column if not exists deleted_by uuid references public.users(id);
create index if not exists patients_not_deleted_idx
  on public.patients(clinic_id) where deleted_at is null;

-- ---------------------------------------------------------------------
-- Read-path indexes the application's actual queries need.
-- ---------------------------------------------------------------------
create index if not exists visits_patient_status_idx
  on public.visits(patient_id, status) where deleted_at is null;
create index if not exists clinical_facts_confirmed_idx
  on public.clinical_facts(patient_id, asserted_at)
  where status = 'confirmed';
