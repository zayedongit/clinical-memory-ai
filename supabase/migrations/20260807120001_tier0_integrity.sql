-- =====================================================================
-- Tier 0 — data integrity & medico-legal hardening
--   0.2  Make audit_log genuinely immutable (trigger-enforced) + hash-chain
--        every row so tampering is detectable, not just discouraged.
--   0.3  Soft-delete clinical records instead of destroying them.
--   0.4  Capture the legal fields a prescription must carry (doctor
--        registration number, qualifications, signature; clinic address).
-- All additive + idempotent. No data loss.
-- =====================================================================

create extension if not exists pgcrypto;   -- digest() for the hash chain

-- ---------------------------------------------------------------------
-- 0.2  audit_log: append-only, tamper-evident
-- ---------------------------------------------------------------------
alter table public.audit_log add column if not exists prev_hash text;
alter table public.audit_log add column if not exists row_hash  text;

-- Hash-chain each row to the previous one for its clinic. Any later edit to
-- an earlier row breaks every subsequent row_hash, making tampering provable.
create or replace function public.audit_log_hash()
returns trigger
language plpgsql
as $$
declare
  prev text;
begin
  -- serialise the chain per clinic so prev_hash is deterministic under concurrency
  perform pg_advisory_xact_lock(hashtext('audit_log:' || coalesce(new.clinic_id::text, 'global')));
  select row_hash into prev
    from public.audit_log
    where clinic_id is not distinct from new.clinic_id
    order by at desc, id desc
    limit 1;
  new.prev_hash := prev;
  new.row_hash := encode(digest(
    coalesce(prev, '') || '|' ||
    coalesce(new.actor_id::text, '') || '|' ||
    new.action || '|' || new.entity || '|' ||
    coalesce(new.entity_id::text, '') || '|' ||
    coalesce(new.before::text, '') || '|' ||
    coalesce(new.after::text, '') || '|' ||
    coalesce(new.at::text, now()::text),
    'sha256'), 'hex');
  return new;
end;
$$;

drop trigger if exists audit_log_hash_t on public.audit_log;
create trigger audit_log_hash_t
  before insert on public.audit_log
  for each row execute function public.audit_log_hash();

-- Block ALL updates and deletes — even the service_role, because triggers
-- fire regardless of RLS. The audit log can only ever grow.
create or replace function public.audit_log_immutable()
returns trigger
language plpgsql
as $$
begin
  raise exception 'audit_log is append-only; % is not permitted', tg_op;
end;
$$;

drop trigger if exists audit_log_no_update on public.audit_log;
create trigger audit_log_no_update
  before update on public.audit_log
  for each row execute function public.audit_log_immutable();

drop trigger if exists audit_log_no_delete on public.audit_log;
create trigger audit_log_no_delete
  before delete on public.audit_log
  for each row execute function public.audit_log_immutable();

revoke update, delete on public.audit_log from anon, authenticated;

-- ---------------------------------------------------------------------
-- 0.3  Soft-delete: retain clinical records, never destroy them.
-- ---------------------------------------------------------------------
alter table public.visits     add column if not exists deleted_at    timestamptz;
alter table public.visits     add column if not exists deleted_by    uuid references public.users(id);
alter table public.visits     add column if not exists delete_reason text;
alter table public.soap_notes add column if not exists deleted_at    timestamptz;
alter table public.soap_notes add column if not exists deleted_by    uuid references public.users(id);

create index if not exists visits_not_deleted_idx     on public.visits(clinic_id) where deleted_at is null;
create index if not exists soap_notes_not_deleted_idx on public.soap_notes(clinic_id) where deleted_at is null;

-- Remove the hard-delete paths. Deletion now means UPDATE ... SET deleted_at.
drop policy if exists patients_delete   on public.patients;
drop policy if exists soap_notes_delete on public.soap_notes;
revoke delete on public.patients, public.visits, public.soap_notes from anon, authenticated;

-- ---------------------------------------------------------------------
-- 0.4  Legal fields a valid prescription must carry.
--      Doctor identity prints on every Rx; clinic identity forms the header.
-- ---------------------------------------------------------------------
alter table public.users add column if not exists registration_no text;   -- SMC/NMC registration
alter table public.users add column if not exists qualifications  text;   -- e.g. MBBS, MD (Gen Med)
alter table public.users add column if not exists signature_url    text;   -- Supabase Storage path

alter table public.clinics add column if not exists address text;
alter table public.clinics add column if not exists phone   text;
