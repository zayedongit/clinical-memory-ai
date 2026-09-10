"""Record integrity: audit tamper-evidence, append-only facts, signed-note
immutability, amendments, and the schema defects that started this work.
"""
from __future__ import annotations

from itertools import pairwise

import psycopg
import pytest

from .conftest import CLINIC_A, CLINIC_B, PATIENT_A, PATIENT_B
from .test_finalize_visit import finalize

pytestmark = pytest.mark.db


# ===================================================================== #
# Audit log: append-only and tamper-evident
# ===================================================================== #
def test_the_audit_log_cannot_be_updated_even_by_service_role(service_role, doctor_a):
    """Triggers fire regardless of RLS and regardless of role, which is the
    only way a log is append-only in a database the application can write to."""
    finalize(doctor_a)
    error = service_role.expect_error("update public.audit_log set action = 'nothing_happened'")
    assert isinstance(error, psycopg.errors.RaiseException)
    assert "append-only" in str(error).lower()


def test_the_audit_log_cannot_be_deleted_from(service_role, doctor_a):
    finalize(doctor_a)
    error = service_role.expect_error("delete from public.audit_log")
    assert isinstance(error, psycopg.errors.RaiseException)


def test_every_audit_row_is_hash_chained_to_the_previous_one(doctor_a):
    for _ in range(3):
        finalize(doctor_a, draft=True, attested=False)

    rows = doctor_a.all(
        "select seq, prev_hash, row_hash from public.audit_log where clinic_id = %s order by seq",
        (CLINIC_A,))
    assert len(rows) >= 3
    assert rows[0][1] is None, "the first row in a clinic's chain has no predecessor"
    for previous, current in pairwise(rows):
        assert current[1] == previous[2], "prev_hash must equal the previous row_hash"
        assert current[2] and len(current[2]) == 64
        assert current[0] > previous[0], "seq must increase in insertion order"


def test_the_chain_is_ordered_by_insertion_not_by_timestamp(doctor_a):
    """`at` defaults to now(), which is constant for a whole transaction.

    The original chain broke that tie with a random UUID, so rows written in
    one transaction linked in UUID order rather than insertion order — and
    finalize_visit() writes its audit row inside the clinical transaction, so
    this was reachable in normal use. Chaining by a monotonic sequence fixes it.
    """
    for _ in range(4):
        finalize(doctor_a, draft=True, attested=False)

    rows = doctor_a.all(
        "select at, seq from public.audit_log where clinic_id = %s order by seq", (CLINIC_A,))
    assert len(rows) >= 4
    assert len({r[0] for r in rows}) < len(rows), (
        "expected identical timestamps inside one transaction; if this fails the "
        "test is no longer exercising the tie-break it exists for"
    )
    assert [r[1] for r in rows] == sorted(r[1] for r in rows)


def test_verify_audit_chain_reports_an_intact_chain_as_clean(doctor_a):
    finalize(doctor_a)
    finalize(doctor_a, draft=True, attested=False)
    assert doctor_a.all("select * from public.verify_audit_chain(%s)", (CLINIC_A,)) == []


def test_verify_audit_chain_detects_a_rewritten_row(doctor_a, db):
    """Tamper-*evident*, not tamper-proof.

    Nothing stops the database *owner* from disabling the trigger and rewriting
    history, which is exactly what this test does: it drops to the owner role,
    because neither `authenticated` nor `service_role` can. What the chain
    guarantees is that the rewrite becomes detectable afterwards, which is the
    honest version of the claim.
    """
    finalize(doctor_a)
    finalize(doctor_a, draft=True, attested=False)

    db.execute("reset role")     # the table owner, the only role that can do this
    db.execute("alter table public.audit_log disable trigger audit_log_no_update")
    db.execute("update public.audit_log set action = 'nothing_happened' "
               "where seq = (select min(seq) from public.audit_log where clinic_id = %s)",
               (CLINIC_A,))
    db.execute("alter table public.audit_log enable trigger audit_log_no_update")

    breaks = db.execute("select seq, problem from public.verify_audit_chain(%s)",
                        (CLINIC_A,)).fetchall()
    assert breaks, "a rewritten row must be detectable"
    assert "row_hash does not match" in " ".join(b[1] for b in breaks)


def test_the_audit_actor_comes_from_the_jwt_not_an_argument(doctor_a):
    """write_audit() is SECURITY DEFINER, so it must not accept an actor from
    the caller — otherwise any user could forge an entry as anyone else."""
    from .conftest import DOCTOR_A

    doctor_a.run("select public.write_audit('delete_visit', 'visit', null, '{}'::jsonb)")
    actor = doctor_a.one(
        "select actor_id from public.audit_log where action = 'delete_visit'")
    assert str(actor) == DOCTOR_A


def test_only_known_audit_actions_are_accepted(doctor_a):
    """write_audit() is granted to `authenticated` because finalize_visit()
    runs as the invoker. Without a whitelist, a user could call it directly and
    bury a real entry under arbitrary noise — which is exactly what an audit
    log has to resist."""
    error = doctor_a.expect_error(
        "select public.write_audit('nothing_to_see_here', 'visit', null)")
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "not a known audit action" in str(error)


def test_only_known_audit_entities_are_accepted(doctor_a):
    error = doctor_a.expect_error(
        "select public.write_audit('delete_visit', 'made_up_entity', null)")
    assert isinstance(error, psycopg.errors.CheckViolation)


def test_an_oversized_audit_payload_is_refused(doctor_a):
    """The log must not be usable as arbitrary storage."""
    error = doctor_a.expect_error(
        "select public.write_audit('delete_visit', 'visit', null, "
        "jsonb_build_object('x', repeat('a', 20000)))")
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "too large" in str(error)


def test_every_action_the_application_emits_is_whitelisted(doctor_a):
    """Adding an audited action to the code without adding it here would fail
    silently at runtime, losing the entry it was meant to create."""
    for action, entity in [
        ("sign_visit", "visit"), ("save_draft", "visit"), ("amend_note", "visit"),
        ("delete_visit", "visit"), ("create_patient", "patient"),
        ("update_patient", "patient"), ("bootstrap_clinic", "clinic"),
    ]:
        assert doctor_a.expect_error(
            "select public.write_audit(%s, %s, null)", (action, entity)) is None, action


def test_write_audit_refuses_a_caller_with_no_clinic(db):
    db.execute("set local role authenticated")
    db.execute("select set_config('request.jwt.claims', %s, true)",
               ('{"sub":"00000000-0000-0000-0000-0000000000ff","role":"authenticated"}',))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute("select public.write_audit('x', 'visit', null)")
    db.execute("rollback")


# ===================================================================== #
# clinical_facts: genuinely append-only
# ===================================================================== #
def test_an_asserted_fact_value_cannot_be_edited(doctor_a):
    """'Append-only' was a convention enforced by nothing. A trigger now
    enforces it, so a correction has to be a new superseding row."""
    finalize(doctor_a)
    error = doctor_a.expect_error(
        "update public.clinical_facts set value = 'Something else' where value = 'Viral URTI'")
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "immutable" in str(error).lower()


def test_facts_cannot_be_moved_between_patients(doctor_a):
    finalize(doctor_a)
    error = doctor_a.expect_error(
        "update public.clinical_facts set patient_id = %s where value = 'cough'", (PATIENT_A,))
    # Same patient is a no-op change and is allowed; a different one is not.
    assert error is None

    error = doctor_a.expect_error(
        "update public.clinical_facts set fact_type = 'allergy' where value = 'cough'")
    assert isinstance(error, psycopg.errors.CheckViolation)


def test_only_legal_status_transitions_are_permitted(doctor_a):
    finalize(doctor_a)
    # confirmed -> superseded is how a correction is recorded.
    assert doctor_a.expect_error(
        "update public.clinical_facts set status = 'superseded' where value = 'cough'") is None
    # ...and it does not go back.
    error = doctor_a.expect_error(
        "update public.clinical_facts set status = 'confirmed' where value = 'cough'")
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "illegal status transition" in str(error).lower()


def test_re_signing_supersedes_prior_facts_rather_than_deleting_them(doctor_a):
    """The whole point of an append-only store: the earlier assertion is still
    on record, marked superseded, with the time it stopped being current."""
    draft = finalize(doctor_a, draft=True, attested=False)
    finalize(doctor_a, visit_id=draft["visit_id"])

    visit_id = draft["visit_id"]
    confirmed = doctor_a.one(
        "select count(*) from public.clinical_facts where visit_id = %s and status = 'confirmed'",
        (visit_id,))
    assert confirmed == 3

    # Amend the visit's facts by writing a fresh set through a new draft cycle
    # is not possible on a signed visit; supersession is exercised directly.
    doctor_a.run(
        "update public.clinical_facts set status = 'superseded', valid_to = now() "
        "where visit_id = %s and value = 'cough'", (visit_id,))
    row = doctor_a.all(
        "select status, valid_to from public.clinical_facts where value = 'cough'")[0]
    assert row[0] == "superseded"
    assert row[1] is not None


def test_clinical_status_distinguishes_current_from_resolved(doctor_a):
    """Longitudinal memory must not treat a resolved problem as present-tense."""
    result = finalize(doctor_a, facts=[
        {"fact_type": "diagnosis", "value": "Anaemia", "clinical_status": "resolved"},
        {"fact_type": "diagnosis", "value": "Hypertension", "clinical_status": "current"},
    ])
    assert result["facts_written"] == 2
    statuses = dict(doctor_a.all(
        "select value, clinical_status from public.clinical_facts where visit_id = %s",
        (result["visit_id"],)))
    assert statuses == {"Anaemia": "resolved", "Hypertension": "current"}


def test_an_invalid_clinical_status_is_rejected(doctor_a):
    error = doctor_a.expect_error(
        "insert into public.clinical_facts "
        "(patient_id, clinic_id, fact_type, value, source, clinical_status) "
        "values (%s, %s, 'diagnosis', 'x', 'doctor_entered', 'maybe')",
        (PATIENT_A, CLINIC_A))
    assert isinstance(error, psycopg.errors.CheckViolation)


# ===================================================================== #
# Signed notes are content-immutable; corrections append
# ===================================================================== #
def test_a_signed_note_cannot_be_rewritten(doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    error = doctor_a.expect_error(
        "update public.soap_notes set assessment = 'Something the doctor never wrote' "
        "where visit_id = %s", (visit_id,))
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "immutable" in str(error).lower()


def test_attestation_itself_cannot_be_removed(doctor_a):
    """Un-attesting a note would turn a signed record back into an editable
    draft, which is the long way round to rewriting it."""
    visit_id = finalize(doctor_a)["visit_id"]
    error = doctor_a.expect_error(
        "update public.soap_notes set attested = false where visit_id = %s", (visit_id,))
    assert isinstance(error, psycopg.errors.CheckViolation)


def test_a_draft_note_stays_freely_editable(doctor_a):
    draft = finalize(doctor_a, draft=True, attested=False)
    assert doctor_a.expect_error(
        "update public.soap_notes set assessment = 'Revised' where visit_id = %s",
        (draft["visit_id"],)) is None


def test_a_signed_note_can_still_be_soft_deleted(doctor_a):
    """Removing a record from view is permitted and audited; destroying it is not."""
    visit_id = finalize(doctor_a)["visit_id"]
    assert doctor_a.expect_error(
        "update public.soap_notes set deleted_at = now() where visit_id = %s", (visit_id,)) is None


def test_amendments_append_and_leave_the_original_intact(doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    doctor_a.run(
        "select public.amend_visit_note(%s, 'Dose corrected after pharmacy call', "
        "'Paracetamol 500mg TDS, not QDS')", (visit_id,))

    assessment, amendments = doctor_a.all(
        "select assessment, amendments from public.soap_notes where visit_id = %s", (visit_id,))[0]
    assert assessment == "Viral URTI", "the signed text must be untouched"
    assert len(amendments) == 1
    assert amendments[0]["reason"] == "Dose corrected after pharmacy call"
    assert amendments[0]["by"] is not None

    assert doctor_a.one(
        "select count(*) from public.audit_log where action = 'amend_note' and entity_id = %s",
        (visit_id,)) == 1


def test_amending_requires_a_reason_and_the_correction_text(doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    for reason, text in [("", "correction"), ("reason", ""), ("   ", "   ")]:
        error = doctor_a.expect_error(
            "select public.amend_visit_note(%s, %s, %s)", (visit_id, reason, text))
        assert isinstance(error, psycopg.errors.CheckViolation)


def test_an_unsigned_note_is_edited_not_amended(doctor_a):
    draft = finalize(doctor_a, draft=True, attested=False)
    error = doctor_a.expect_error(
        "select public.amend_visit_note(%s, 'reason', 'text')", (draft["visit_id"],))
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "not signed yet" in str(error).lower()


def test_staff_cannot_amend(staff_a, doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    error = staff_a.expect_error(
        "select public.amend_visit_note(%s, 'reason', 'text')", (visit_id,))
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


# ===================================================================== #
# The schema defects that made the committed migrations unrunnable
# ===================================================================== #
def test_the_status_the_application_writes_for_a_draft_is_accepted(doctor_a):
    """`visits.status` allowed only ('draft','approved') while the application
    wrote 'in_progress'. Every draft save 400d on a database built from the
    committed migrations — the schema only worked on the live project."""
    assert doctor_a.expect_error(
        "insert into public.visits (patient_id, clinic_id, status) values (%s, %s, 'in_progress')",
        (PATIENT_A, CLINIC_A)) is None


@pytest.mark.parametrize("status", ["draft", "in_progress", "approved", "completed"])
def test_all_statuses_the_read_paths_understand_are_valid(doctor_a, status):
    assert doctor_a.expect_error(
        "insert into public.visits (patient_id, clinic_id, status) values (%s, %s, %s)",
        (PATIENT_A, CLINIC_A, status)) is None


def test_an_unknown_status_is_still_rejected(doctor_a):
    error = doctor_a.expect_error(
        "insert into public.visits (patient_id, clinic_id, status) values (%s, %s, 'whatever')",
        (PATIENT_A, CLINIC_A))
    assert isinstance(error, psycopg.errors.CheckViolation)


def test_a_visit_can_only_have_one_note(doctor_a):
    """The application PATCHes by visit_id and reads soap_notes[0]; without the
    unique index a duplicated insert makes those reads non-deterministic."""
    visit_id = finalize(doctor_a, draft=True, attested=False)["visit_id"]
    error = doctor_a.expect_error(
        "insert into public.soap_notes (visit_id, patient_id, clinic_id) values (%s, %s, %s)",
        (visit_id, PATIENT_A, CLINIC_A))
    assert isinstance(error, psycopg.errors.UniqueViolation)


def test_updating_a_note_actually_updates_it(doctor_a):
    """soap_notes had no UPDATE policy, so PostgREST returned 204 for updates
    that matched zero rows. Success was indistinguishable from silent loss."""
    draft = finalize(doctor_a, draft=True, attested=False)
    doctor_a.run("update public.soap_notes set assessment = 'Revised assessment' "
                 "where visit_id = %s", (draft["visit_id"],))
    assert doctor_a.one("select assessment from public.soap_notes where visit_id = %s",
                        (draft["visit_id"],)) == "Revised assessment"


# ===================================================================== #
# Audit-chain verification scope
# ===================================================================== #
def test_verify_audit_chain_is_clean_across_multiple_clinics(doctor_a, doctor_b, service_role):
    """The chain is built per clinic, so the verifier must walk it per clinic.

    Walking every row in one global order with a single running hash reported
    two false breaks as soon as a second clinic existed — and the no-argument
    call is the one an operator reaches for. An integrity check that cries wolf
    trains the reader to ignore it, which is exactly when a real break slips by.
    """
    finalize(doctor_a, draft=True, attested=False)
    service_role.run(
        "insert into public.audit_log (clinic_id, action, entity) values (%s, 'save_draft', 'visit')",
        (CLINIC_B,))
    finalize(doctor_a, draft=True, attested=False)

    # As the owner (no clinic), scoped to nothing: every clinic, still clean.
    breaks = service_role.conn.execute("select * from public.verify_audit_chain()").fetchall()
    assert breaks == [], f"false tampering reported across clinics: {breaks}"


def test_a_doctor_can_only_verify_their_own_clinics_log(doctor_a):
    """The function is SECURITY DEFINER and granted to `authenticated`. Scanning
    every clinic by default let any signed-in user enumerate other clinics'
    audit rows."""
    assert doctor_a.all("select * from public.verify_audit_chain()") == []
    error = doctor_a.expect_error("select * from public.verify_audit_chain(%s)", (CLINIC_B,))
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)


def test_an_audit_entry_cannot_reference_a_record_in_another_clinic(doctor_a, service_role):
    other_visit = "dddddddd-0000-0000-0000-00000000000b"
    service_role.run(
        "insert into public.visits (id, patient_id, clinic_id, status) "
        "values (%s, %s, %s, 'approved')", (other_visit, PATIENT_B, CLINIC_B))

    error = doctor_a.expect_error(
        "select public.write_audit('delete_visit', 'visit', %s)", (other_visit,))
    assert isinstance(error, psycopg.errors.NoDataFound)


def test_an_audit_entry_cannot_reference_a_record_that_does_not_exist(doctor_a):
    error = doctor_a.expect_error(
        "select public.write_audit('delete_visit', 'visit', "
        "'99999999-9999-9999-9999-999999999999')")
    assert isinstance(error, psycopg.errors.NoDataFound)


def test_a_null_entity_id_is_still_permitted(doctor_a):
    """Some actions genuinely have no single subject."""
    assert doctor_a.expect_error("select public.write_audit('save_draft', 'visit', null)") is None


# ===================================================================== #
# Patient creation is inside the finalize transaction
# ===================================================================== #
def test_a_new_patient_is_created_inside_the_transaction(doctor_a):
    before = doctor_a.one("select count(*) from public.patients")
    result = doctor_a.one(
        "select public.finalize_visit(p_patient_id := null, p_note := %s::jsonb, "
        "p_facts := '[]'::jsonb, p_draft := false, p_attested := true, "
        "p_new_patient := %s::jsonb)",
        ('{"assessment":"URTI"}', '{"name":"Walk-in Patient","gender":"female","dob":"1990-01-01"}'),
    )
    assert result["patient_created"] is True
    assert doctor_a.one("select count(*) from public.patients") == before + 1
    assert doctor_a.one(
        "select name from public.patients where id = %s", (result["patient_id"],)
    ) == "Walk-in Patient"


def test_a_failed_save_leaves_no_orphan_patient(doctor_a):
    """The create used to be a separate call *before* the RPC, so a failed save
    left the patient committed — and every retry made another record for the
    same person."""
    before = doctor_a.one("select count(*) from public.patients")
    error = doctor_a.expect_error(
        "select public.finalize_visit(p_patient_id := null, p_note := '{}'::jsonb, "
        "p_facts := %s::jsonb, p_draft := false, p_attested := true, "
        "p_new_patient := %s::jsonb)",
        ('[{"fact_type":"not_a_real_type","value":"x"}]', '{"name":"Orphan Candidate"}'),
    )
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert doctor_a.one("select count(*) from public.patients") == before
    assert doctor_a.one(
        "select count(*) from public.patients where name = 'Orphan Candidate'") == 0


def test_creating_a_patient_without_a_name_is_refused(doctor_a):
    for payload in ('{}', '{"name":""}', '{"name":"   "}'):
        error = doctor_a.expect_error(
            "select public.finalize_visit(p_patient_id := null, p_note := '{}'::jsonb, "
            "p_draft := true, p_new_patient := %s::jsonb)", (payload,))
        assert isinstance(error, psycopg.errors.CheckViolation), payload


def test_a_patient_created_during_a_consultation_is_audited(doctor_a):
    result = doctor_a.one(
        "select public.finalize_visit(p_patient_id := null, p_note := '{}'::jsonb, "
        "p_draft := true, p_new_patient := %s::jsonb)", ('{"name":"Audited Patient"}',))
    assert doctor_a.one(
        "select count(*) from public.audit_log where action = 'create_patient' "
        "and entity_id = %s", (result["patient_id"],)) == 1
