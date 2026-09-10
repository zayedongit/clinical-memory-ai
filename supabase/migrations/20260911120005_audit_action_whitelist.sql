-- =====================================================================
-- Constrain what can be written to the audit log.
--
-- `write_audit()` is SECURITY DEFINER and granted to `authenticated`,
-- because finalize_visit() and amend_visit_note() run as the invoker and
-- therefore need the caller to hold EXECUTE on it. It already stamps
-- clinic_id and actor_id from the caller's own JWT, so nobody can forge an
-- entry for another clinic or attributed to another user.
--
-- What it did *not* constrain was the action itself. An authenticated user
-- could call it directly with an arbitrary action string and payload,
-- attributed to themselves in their own clinic — enough to bury a real
-- entry in noise, which is exactly what an audit log must resist.
--
-- Two changes:
--   1. A whitelist of known actions, checked inside write_audit(). Adding
--      an audited action is now a deliberate schema change, which also
--      catches a typo in application code before it reaches the log.
--   2. A length cap on the payload, so the log cannot be inflated with
--      megabytes of arbitrary JSON.
--
-- The service-role path (used by the API for patient CRUD auditing) writes
-- to the table directly and is unaffected; it is server-side only.
-- =====================================================================

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
  me     record;
  new_id uuid;
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
