-- =====================================================================
-- Two corrections to the audit layer.
--
-- 1. verify_audit_chain() reported false tampering across clinics.
--
--    The chain is built *per clinic* (audit_log_hash picks its predecessor
--    `where clinic_id is not distinct from new.clinic_id`), but the verifier
--    walked every row in one global `order by seq` with a single `prev`
--    variable. With clinic A at seq 1 and 3 and clinic B at seq 2, row 2's
--    prev_hash (null — first in B's chain) did not match A's row-1 hash, and
--    row 3's did not match B's. Both were reported as breaks.
--
--    So the no-argument call — the one an operator reaches for — declared an
--    intact log tampered as soon as a second clinic existed. An integrity
--    check that cries wolf is worse than none: it trains the reader to ignore
--    the output, which is exactly when a real break slips past.
--
--    Fixed by resetting the running hash at every clinic boundary, the same
--    way the re-chaining block in 20260911120004 already did.
--
-- 2. verify_audit_chain() leaked other clinics' audit metadata.
--
--    It is SECURITY DEFINER, granted to `authenticated`, and defaulted to
--    scanning every clinic — so any signed-in user could enumerate the seq
--    and id of every audit row in the database. It now defaults to the
--    caller's own clinic and refuses any other.
--
-- 3. write_audit() accepted an arbitrary entity_id.
--
--    The action/entity whitelist stopped fabricated *kinds* of entry, but the
--    entity_id was still caller-controlled, so an entry could be attached to a
--    record that does not exist or belongs to nobody. It is now required to
--    resolve to a row the caller can see.
-- =====================================================================

-- The result columns change (clinic_id is added), and a return type cannot be
-- replaced in place.
drop function if exists public.verify_audit_chain(uuid);

create function public.verify_audit_chain(p_clinic_id uuid default null)
returns table (clinic_id uuid, seq bigint, id uuid, problem text)
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  me           record;
  target       uuid;
  r            record;
  prev         text := null;
  expected     text;
  last_clinic  uuid;
  started      boolean := false;
begin
  select * into me from public.current_user_row();

  -- A caller with no clinic is either an operator (service_role, or a direct
  -- psql session) or a web session that has no business reading an audit trail.
  --
  -- The check reads the `role` GUC rather than current_user: inside a SECURITY
  -- DEFINER function current_user is the function's *owner*, so it would say
  -- "postgres" for every caller and let anyone through. The GUC reflects the
  -- SET ROLE that PostgREST performs, and is 'none' for a direct connection.
  if me.clinic_id is null then
    if coalesce(current_setting('role', true), 'none') in ('authenticated', 'anon') then
      raise exception 'verify_audit_chain: caller is not linked to a clinic'
        using errcode = 'insufficient_privilege';
    end if;
    target := p_clinic_id;                       -- operator may scope freely
  else
    target := coalesce(p_clinic_id, me.clinic_id);
    if target is distinct from me.clinic_id then
      raise exception 'verify_audit_chain: you may only verify your own clinic''s log'
        using errcode = 'insufficient_privilege';
    end if;
  end if;

  for r in
    select a.clinic_id, a.id, a.seq, a.prev_hash, a.row_hash, a.actor_id, a.action,
           a.entity, a.entity_id, a.before, a.after, a.at
    from public.audit_log a
    where target is null or a.clinic_id is not distinct from target
    order by a.clinic_id nulls first, a.seq
  loop
    -- The chain restarts at every clinic boundary, because that is how it is
    -- written.
    if not started or r.clinic_id is distinct from last_clinic then
      prev := r.prev_hash;
      last_clinic := r.clinic_id;
      started := true;
    elsif r.prev_hash is distinct from prev then
      clinic_id := r.clinic_id; seq := r.seq; id := r.id;
      problem := 'prev_hash does not match the previous row';
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
      clinic_id := r.clinic_id; seq := r.seq; id := r.id;
      problem := 'row_hash does not match the row contents';
      return next;
    end if;

    prev := r.row_hash;
  end loop;
  return;
end;
$$;

grant execute on function public.verify_audit_chain(uuid) to authenticated;

-- ---------------------------------------------------------------------
-- write_audit: the entity must actually exist and be visible to the caller.
-- ---------------------------------------------------------------------
create or replace function public.write_audit(
  p_action    text,
  p_entity    text,
  p_entity_id uuid,
  p_after     jsonb default null,
  p_before    jsonb default null
)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
  me      record;
  new_id  uuid;
  exists_ boolean;
  allowed constant text[] := array[
    'sign_visit', 'save_draft', 'amend_note', 'delete_visit',
    'create_patient', 'update_patient', 'bootstrap_clinic'
  ];
begin
  select * into me from public.current_user_row();
  if me.user_id is null then
    raise exception 'write_audit: caller is not linked to a clinic'
      using errcode = 'insufficient_privilege';
  end if;

  if not (p_action = any(allowed)) then
    raise exception 'write_audit: % is not a known audit action', p_action
      using errcode = 'check_violation';
  end if;

  if p_entity not in ('visit', 'patient', 'clinic', 'note') then
    raise exception 'write_audit: % is not a known entity', p_entity
      using errcode = 'check_violation';
  end if;

  -- An entry pointing at a record that does not exist, or that belongs to
  -- another clinic, is noise in an audit trail at best and a forged reference
  -- at worst. A null entity_id stays permitted: some actions genuinely have
  -- no single subject.
  if p_entity_id is not null then
    exists_ := case p_entity
      when 'visit'   then exists (select 1 from public.visits   where id = p_entity_id
                                    and clinic_id = me.clinic_id)
      when 'patient' then exists (select 1 from public.patients where id = p_entity_id
                                    and clinic_id = me.clinic_id)
      when 'note'    then exists (select 1 from public.soap_notes where id = p_entity_id
                                    and clinic_id = me.clinic_id)
      when 'clinic'  then p_entity_id = me.clinic_id
      else false
    end;
    if not exists_ then
      raise exception 'write_audit: % % is not a record in this clinic', p_entity, p_entity_id
        using errcode = 'no_data_found';
    end if;
  end if;

  -- 16 KB is generous for a change summary and small enough that the log
  -- cannot be used as arbitrary storage.
  if length(coalesce(p_after::text, '')) + length(coalesce(p_before::text, '')) > 16384 then
    raise exception 'write_audit: payload too large for an audit entry'
      using errcode = 'check_violation';
  end if;

  insert into public.audit_log (clinic_id, actor_id, action, entity, entity_id, before, after)
  values (me.clinic_id, me.user_id, p_action, p_entity, p_entity_id, p_before, p_after)
  returning id into new_id;

  return new_id;
end;
$$;

grant execute on function public.write_audit(text, text, uuid, jsonb, jsonb) to authenticated;
