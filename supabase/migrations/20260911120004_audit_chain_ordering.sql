-- =====================================================================
-- Audit hash chain: order by a monotonic sequence, not by timestamp.
--
-- The chain trigger picked its predecessor with
--     order by at desc, id desc limit 1
-- `at` defaults to now(), which is the *transaction* start time and is
-- therefore identical for every row written in one transaction. The tie
-- was then broken by `id`, a random UUID — so within a transaction (and
-- within any clock tick) the chain linked rows in UUID order rather than
-- insertion order.
--
-- Consequences: two rows could claim the same predecessor, the chain
-- could not be walked deterministically, and verification could not
-- distinguish "tampered" from "written in the same transaction". Since
-- finalize_visit() writes an audit row inside the same transaction as the
-- clinical write, this was reachable in normal operation.
--
-- Fix: a per-clinic monotonic sequence. `seq` is assigned from a database
-- sequence, so it is strictly increasing in insertion order regardless of
-- timestamps, and the chain is walked by it.
--
-- Existing rows are backfilled in (at, id) order — the best reconstruction
-- available — and re-chained so the whole log verifies under the new rule.
-- =====================================================================

create sequence if not exists public.audit_log_seq;

alter table public.audit_log add column if not exists seq bigint;

-- Backfill in the closest thing to insertion order we can recover.
do $$
declare
  r record;
  n bigint := 0;
begin
  if exists (select 1 from public.audit_log where seq is null) then
    for r in select id from public.audit_log order by at, id loop
      n := n + 1;
      update public.audit_log set seq = n where id = r.id;
    end loop;
    perform setval('public.audit_log_seq', greatest(n, 1));
  end if;
end $$;

alter table public.audit_log alter column seq set default nextval('public.audit_log_seq');
alter table public.audit_log alter column seq set not null;

create unique index if not exists audit_log_seq_uidx on public.audit_log(seq);
create index if not exists audit_log_clinic_seq_idx on public.audit_log(clinic_id, seq);

-- ---------------------------------------------------------------------
-- Chain by seq.
-- ---------------------------------------------------------------------
create or replace function public.audit_log_hash()
returns trigger
language plpgsql
as $$
declare
  prev text;
begin
  if new.seq is null then
    new.seq := nextval('public.audit_log_seq');
  end if;

  -- Serialise the chain per clinic so prev_hash is deterministic under
  -- concurrent inserts from different sessions.
  perform pg_advisory_xact_lock(hashtext('audit_log:' || coalesce(new.clinic_id::text, 'global')));

  select row_hash into prev
    from public.audit_log
    where clinic_id is not distinct from new.clinic_id
      and seq < new.seq
    order by seq desc
    limit 1;

  new.prev_hash := prev;
  new.row_hash := encode(digest(
    coalesce(prev, '') || '|' ||
    new.seq::text || '|' ||
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

-- Re-chain any rows written under the old rule so the whole log verifies.
do $$
declare
  r record;
  prev text;
  last_clinic uuid;
  started boolean := false;
begin
  for r in
    select id, clinic_id, seq, actor_id, action, entity, entity_id, before, after, at
    from public.audit_log
    order by clinic_id nulls first, seq
  loop
    if not started or r.clinic_id is distinct from last_clinic then
      prev := null;
      last_clinic := r.clinic_id;
      started := true;
    end if;

    update public.audit_log
       set prev_hash = prev,
           row_hash = encode(digest(
             coalesce(prev, '') || '|' || r.seq::text || '|' ||
             coalesce(r.actor_id::text, '') || '|' ||
             r.action || '|' || r.entity || '|' ||
             coalesce(r.entity_id::text, '') || '|' ||
             coalesce(r.before::text, '') || '|' ||
             coalesce(r.after::text, '') || '|' ||
             coalesce(r.at::text, ''), 'sha256'), 'hex')
     where id = r.id;

    select row_hash into prev from public.audit_log where id = r.id;
  end loop;
end $$;

-- ---------------------------------------------------------------------
-- Chain verification, so "tamper-evident" is a query rather than a claim.
-- Returns one row per break. An intact chain returns nothing.
-- ---------------------------------------------------------------------
create or replace function public.verify_audit_chain(p_clinic_id uuid default null)
returns table (seq bigint, id uuid, problem text)
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  r record;
  prev text := null;
  expected text;
  first_row boolean := true;
begin
  for r in
    select a.id, a.seq, a.prev_hash, a.row_hash, a.actor_id, a.action, a.entity,
           a.entity_id, a.before, a.after, a.at
    from public.audit_log a
    where p_clinic_id is null or a.clinic_id = p_clinic_id
    order by a.seq
  loop
    if first_row then
      prev := r.prev_hash;      -- accept whatever the chain starts from
      first_row := false;
    elsif r.prev_hash is distinct from prev then
      seq := r.seq; id := r.id; problem := 'prev_hash does not match the previous row';
      return next;
    end if;

    expected := encode(digest(
      coalesce(r.prev_hash, '') || '|' || r.seq::text || '|' ||
      coalesce(r.actor_id::text, '') || '|' ||
      r.action || '|' || r.entity || '|' ||
      coalesce(r.entity_id::text, '') || '|' ||
      coalesce(r.before::text, '') || '|' ||
      coalesce(r.after::text, '') || '|' ||
      coalesce(r.at::text, ''), 'sha256'), 'hex');

    if expected is distinct from r.row_hash then
      seq := r.seq; id := r.id; problem := 'row_hash does not match the row contents';
      return next;
    end if;

    prev := r.row_hash;
  end loop;
  return;
end;
$$;

grant execute on function public.verify_audit_chain(uuid) to authenticated;
