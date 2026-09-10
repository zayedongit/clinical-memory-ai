-- =====================================================================
-- Transactional finalize.
--
-- Signing a consultation used to be five independent PostgREST calls
-- (update visit, update note, supersede facts, insert facts, write audit).
-- A failure part-way through left an approved visit with a stale note, or
-- confirmed facts with no note, or a signed record with no audit entry.
--
-- finalize_visit() does the whole thing in one database transaction, and
-- moves the rules that must not be bypassable into the database:
--
--   * physician attestation is required to approve         (fail-closed)
--   * only a user with role 'doctor' may attest            (authorisation)
--   * an already-approved visit cannot be re-finalised     (immutability)
--   * optimistic locking rejects a concurrent finalize     (no lost update)
--   * the caller's clinic comes from their JWT, never the request body
--
-- SECURITY INVOKER (the default) on purpose: the function runs as the
-- caller, so every RLS policy still applies and the function cannot be
-- used as a tenant-isolation bypass.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Who is calling? Resolves the caller's own users row from their JWT.
-- SECURITY DEFINER only so it can read public.users under RLS; it can
-- never return anyone but the caller.
-- ---------------------------------------------------------------------
create or replace function public.current_user_row()
returns table (user_id uuid, clinic_id uuid, role text)
language sql
stable
security definer
set search_path = public
as $$
  select id, clinic_id, role
  from public.users
  where auth_uid = auth.uid()
  limit 1;
$$;
grant execute on function public.current_user_row() to authenticated;

-- ---------------------------------------------------------------------
-- Audit writes from inside a transaction.
--
-- audit_log has no INSERT policy for `authenticated` by design, so the
-- log cannot be forged from an ordinary session. This SECURITY DEFINER
-- wrapper is the only authenticated write path, and it stamps clinic_id
-- and actor_id from the caller's own JWT rather than trusting arguments.
-- The append-only + hash-chain triggers still apply.
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
  me record;
  new_id uuid;
begin
  select * into me from public.current_user_row();
  if me.user_id is null then
    raise exception 'write_audit: caller is not linked to a clinic' using errcode = 'insufficient_privilege';
  end if;

  insert into public.audit_log (clinic_id, actor_id, action, entity, entity_id, before, after)
  values (me.clinic_id, me.user_id, p_action, p_entity, p_entity_id, p_before, p_after)
  returning id into new_id;

  return new_id;
end;
$$;
grant execute on function public.write_audit(text, text, uuid, jsonb, jsonb) to authenticated;

-- ---------------------------------------------------------------------
-- finalize_visit
--
-- p_visit_id         existing draft to finalise, or null to create
-- p_expected_version version the client last read; null skips the check
--                    (only valid when creating a brand-new visit)
-- p_note             the note columns as a json object
-- p_facts            [{fact_type, value, structured, clinical_status}]
-- p_draft            true  -> save as an in-progress draft (no attestation)
--                    false -> approve (attestation + doctor role required)
-- ---------------------------------------------------------------------
create or replace function public.finalize_visit(
  p_patient_id       uuid,
  p_note             jsonb,
  p_facts            jsonb    default '[]'::jsonb,
  p_visit_id         uuid     default null,
  p_expected_version integer  default null,
  p_draft            boolean  default false,
  p_attested         boolean  default false,
  p_consent_given    boolean  default false,
  p_consent_method   text     default null
)
returns jsonb
language plpgsql
as $$
declare
  me            record;
  v             record;
  v_id          uuid;
  v_status      text;
  v_now         timestamptz := now();
  fact          jsonb;
  fact_count    integer := 0;
  patient_clinic uuid;
begin
  select * into me from public.current_user_row();
  if me.user_id is null then
    raise exception 'Caller is not linked to a clinic' using errcode = 'insufficient_privilege';
  end if;

  -- Fail closed: a permanent clinical record requires an explicit attestation
  -- from a user whose role may attest. The API enforces this too; here it
  -- cannot be bypassed by calling the database directly.
  if not p_draft then
    if not p_attested then
      raise exception 'Physician attestation is required before completing a note'
        using errcode = 'check_violation';
    end if;
    if me.role is distinct from 'doctor' then
      raise exception 'Role % may not attest a clinical note; only a doctor may sign', me.role
        using errcode = 'insufficient_privilege';
    end if;
  end if;

  v_status := case when p_draft then 'in_progress' else 'approved' end;

  -- The patient must be visible to the caller. RLS makes this clinic-scoped,
  -- so a patient_id from another clinic simply does not exist here.
  select clinic_id into patient_clinic from public.patients
   where id = p_patient_id and deleted_at is null;
  if patient_clinic is null then
    raise exception 'Patient % not found in this clinic', p_patient_id using errcode = 'no_data_found';
  end if;

  if p_visit_id is not null then
    -- Lock the row for the rest of the transaction: a concurrent finalize
    -- blocks here and then fails the version check below.
    select id, status, version into v
      from public.visits
     where id = p_visit_id and deleted_at is null
     for update;

    if v.id is null then
      raise exception 'Visit % not found', p_visit_id using errcode = 'no_data_found';
    end if;

    if v.status in ('approved', 'completed') then
      raise exception 'Visit % is already signed; record a correction as an amendment', p_visit_id
        using errcode = 'check_violation';
    end if;

    if p_expected_version is not null and v.version is distinct from p_expected_version then
      raise exception
        'Visit % was modified by someone else (expected version %, found %). Reload and try again.',
        p_visit_id, p_expected_version, v.version
        using errcode = 'serialization_failure';
    end if;

    update public.visits
       set status      = v_status,
           approved_at = case when p_draft then null else v_now end,
           consent_given  = case when p_consent_given then true else consent_given end,
           consent_at     = case when p_consent_given and consent_at is null then v_now else consent_at end,
           consent_method = coalesce(nullif(p_consent_method, ''), consent_method)
     where id = p_visit_id;
    v_id := p_visit_id;
  else
    insert into public.visits (
      patient_id, clinic_id, doctor_id, status, approved_at,
      consent_given, consent_at, consent_method
    ) values (
      p_patient_id, me.clinic_id, me.user_id, v_status,
      case when p_draft then null else v_now end,
      p_consent_given,
      case when p_consent_given then v_now else null end,
      nullif(p_consent_method, '')
    ) returning id into v_id;
  end if;

  -- One note per visit (enforced by soap_notes_visit_uidx).
  insert into public.soap_notes (
    visit_id, patient_id, clinic_id, created_by,
    transcript, dialogue, subjective, objective, assessment, plan,
    entities, follow_up_questions, prescription, clinical_considerations,
    vitals, wizard, sign_off, attested, attested_at, attested_by
  ) values (
    v_id, p_patient_id, me.clinic_id, me.user_id,
    p_note ->> 'transcript',
    coalesce(p_note -> 'dialogue', '[]'::jsonb),
    p_note ->> 'subjective', p_note ->> 'objective',
    p_note ->> 'assessment', p_note ->> 'plan',
    coalesce(p_note -> 'entities', '{}'::jsonb),
    coalesce(p_note -> 'follow_up_questions', '[]'::jsonb),
    coalesce(p_note -> 'prescription', '[]'::jsonb),
    coalesce(p_note -> 'clinical_considerations', '{}'::jsonb),
    coalesce(p_note -> 'vitals', '{}'::jsonb),
    coalesce(p_note -> 'wizard', '{}'::jsonb),
    coalesce(p_note -> 'sign_off', '{}'::jsonb),
    not p_draft,
    case when p_draft then null else v_now end,
    case when p_draft then null else me.user_id end
  )
  on conflict (visit_id) do update set
    transcript              = excluded.transcript,
    dialogue                = excluded.dialogue,
    subjective              = excluded.subjective,
    objective               = excluded.objective,
    assessment              = excluded.assessment,
    plan                    = excluded.plan,
    entities                = excluded.entities,
    follow_up_questions     = excluded.follow_up_questions,
    prescription            = excluded.prescription,
    clinical_considerations = excluded.clinical_considerations,
    vitals                  = excluded.vitals,
    wizard                  = excluded.wizard,
    sign_off                = excluded.sign_off,
    attested                = excluded.attested,
    attested_at             = excluded.attested_at,
    attested_by             = excluded.attested_by;

  -- Doctor-approved facts become longitudinal memory. Drafts contribute
  -- nothing: an unsigned note must never look like confirmed history.
  if not p_draft then
    update public.clinical_facts
       set status = 'superseded', valid_to = v_now
     where visit_id = v_id and status = 'confirmed';

    for fact in select * from jsonb_array_elements(coalesce(p_facts, '[]'::jsonb))
    loop
      if coalesce(trim(fact ->> 'value'), '') = '' then
        continue;
      end if;
      insert into public.clinical_facts (
        clinic_id, patient_id, visit_id, fact_type, value, structured,
        source, status, clinical_status, asserted_by, asserted_at
      ) values (
        me.clinic_id, p_patient_id, v_id,
        fact ->> 'fact_type',
        left(fact ->> 'value', 500),
        coalesce(fact -> 'structured', '{}'::jsonb),
        coalesce(fact ->> 'source', 'doctor_confirmed_ai'),
        'confirmed',
        coalesce(fact ->> 'clinical_status', 'current'),
        me.user_id, v_now
      );
      fact_count := fact_count + 1;
    end loop;
  end if;

  perform public.write_audit(
    case when p_draft then 'save_draft' else 'sign_visit' end,
    'visit', v_id,
    jsonb_build_object(
      'patient_id', p_patient_id,
      'status',     v_status,
      'facts',      fact_count,
      'overrides',  coalesce(p_note -> 'sign_off' -> 'overrides', '[]'::jsonb)
    )
  );

  return jsonb_build_object(
    'visit_id',   v_id,
    'patient_id', p_patient_id,
    'status',     case when p_draft then 'in_progress' else 'approved' end,
    'version',    (select version from public.visits where id = v_id),
    'facts_written', fact_count
  );
end;
$$;

grant execute on function public.finalize_visit(
  uuid, jsonb, jsonb, uuid, integer, boolean, boolean, boolean, text
) to authenticated;

-- ---------------------------------------------------------------------
-- amend_visit_note — the only way to correct a signed note.
-- The original text is never rewritten; the correction is appended with
-- its author, timestamp and reason, and the audit log records it.
-- ---------------------------------------------------------------------
create or replace function public.amend_visit_note(
  p_visit_id uuid,
  p_reason   text,
  p_text     text
)
returns jsonb
language plpgsql
as $$
declare
  me     record;
  n      record;
  entry  jsonb;
begin
  select * into me from public.current_user_row();
  if me.user_id is null then
    raise exception 'Caller is not linked to a clinic' using errcode = 'insufficient_privilege';
  end if;
  if me.role is distinct from 'doctor' then
    raise exception 'Role % may not amend a clinical note', me.role using errcode = 'insufficient_privilege';
  end if;
  if coalesce(trim(p_reason), '') = '' or coalesce(trim(p_text), '') = '' then
    raise exception 'An amendment needs both a reason and the correction text'
      using errcode = 'check_violation';
  end if;

  select id, attested, amendments into n
    from public.soap_notes
   where visit_id = p_visit_id and deleted_at is null;

  if n.id is null then
    raise exception 'Note for visit % not found', p_visit_id using errcode = 'no_data_found';
  end if;
  if not coalesce(n.attested, false) then
    raise exception 'Visit % is not signed yet; edit the draft instead', p_visit_id
      using errcode = 'check_violation';
  end if;

  entry := jsonb_build_object(
    'at', now(), 'by', me.user_id, 'reason', p_reason, 'text', p_text
  );

  update public.soap_notes
     set amendments = coalesce(amendments, '[]'::jsonb) || jsonb_build_array(entry)
   where id = n.id;

  perform public.write_audit('amend_note', 'visit', p_visit_id, entry);

  return entry;
end;
$$;

grant execute on function public.amend_visit_note(uuid, text, text) to authenticated;
