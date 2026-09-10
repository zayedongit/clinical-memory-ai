-- =====================================================================
-- Bring patient creation inside the finalize transaction.
--
-- `/scribe/save` with a `new_patient` inserted the patient through a
-- separate PostgREST call *before* calling finalize_visit(). If the RPC
-- then failed — already signed, version conflict, upstream error — the
-- patient row was already committed, and the UI simply showed "Save
-- failed" and stayed open. Every retry created another patient record for
-- the same person, which is exactly the duplicate-record problem the UHID
-- and merge machinery exists to avoid.
--
-- finalize_visit() already runs as the caller with their clinic resolved
-- from the JWT, so it can create the patient itself and have the whole
-- thing roll back together.
--
-- The new argument is last and defaults to null, so existing calls are
-- unaffected.
-- =====================================================================

create or replace function public.finalize_visit(
  p_patient_id       uuid,
  p_note             jsonb,
  p_facts            jsonb    default '[]'::jsonb,
  p_visit_id         uuid     default null,
  p_expected_version integer  default null,
  p_draft            boolean  default false,
  p_attested         boolean  default false,
  p_consent_given    boolean  default false,
  p_consent_method   text     default null,
  p_new_patient      jsonb    default null
)
returns jsonb
language plpgsql
as $$
declare
  me            record;
  v             record;
  v_id          uuid;
  patient_id    uuid := p_patient_id;
  v_status      text;
  v_now         timestamptz := now();
  fact          jsonb;
  fact_count    integer := 0;
  patient_clinic uuid;
  created_patient boolean := false;
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

  -- Create the patient inside this transaction when one was not supplied, so
  -- a later failure rolls the patient back with everything else.
  if patient_id is null then
    if p_new_patient is null or coalesce(trim(p_new_patient ->> 'name'), '') = '' then
      raise exception 'Provide either an existing patient or a new patient with a name'
        using errcode = 'check_violation';
    end if;
    insert into public.patients (clinic_id, name, gender, phone, dob, height_cm, weight_kg)
    values (
      me.clinic_id,
      left(trim(p_new_patient ->> 'name'), 200),
      nullif(p_new_patient ->> 'gender', ''),
      nullif(p_new_patient ->> 'phone', ''),
      nullif(p_new_patient ->> 'dob', '')::date,
      nullif(p_new_patient ->> 'height_cm', '')::numeric,
      nullif(p_new_patient ->> 'weight_kg', '')::numeric
    )
    returning id into patient_id;
    created_patient := true;
  end if;

  -- The patient must be visible to the caller. RLS makes this clinic-scoped,
  -- so a patient_id from another clinic simply does not exist here.
  select clinic_id into patient_clinic from public.patients
   where id = patient_id and deleted_at is null;
  if patient_clinic is null then
    raise exception 'Patient % not found in this clinic', patient_id using errcode = 'no_data_found';
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
      patient_id, me.clinic_id, me.user_id, v_status,
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
    v_id, patient_id, me.clinic_id, me.user_id,
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
        me.clinic_id, patient_id, v_id,
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

  if created_patient then
    perform public.write_audit('create_patient', 'patient', patient_id,
                               jsonb_build_object('via', 'consultation'));
  end if;

  perform public.write_audit(
    case when p_draft then 'save_draft' else 'sign_visit' end,
    'visit', v_id,
    jsonb_build_object(
      'patient_id', patient_id,
      'status',     v_status,
      'facts',      fact_count,
      'overrides',  coalesce(p_note -> 'sign_off' -> 'overrides', '[]'::jsonb)
    )
  );

  return jsonb_build_object(
    'visit_id',   v_id,
    'patient_id', patient_id,
    'status',     case when p_draft then 'in_progress' else 'approved' end,
    'version',    (select version from public.visits where id = v_id),
    'facts_written', fact_count,
    'patient_created', created_patient
  );
end;
$$;

grant execute on function public.finalize_visit(
  uuid, jsonb, jsonb, uuid, integer, boolean, boolean, boolean, text, jsonb
) to authenticated;

-- The previous 9-argument signature is gone: PostgREST resolves an RPC by the
-- names in the request body, and leaving both would make the choice ambiguous.
drop function if exists public.finalize_visit(
  uuid, jsonb, jsonb, uuid, integer, boolean, boolean, boolean, text
);
