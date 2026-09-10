"""finalize_visit(): the rules that must not be bypassable.

Attestation, role, immutability of a signed record and optimistic locking are
all enforced inside one database transaction. The API checks them too, but the
API can be wrong, refactored, or bypassed by anyone with a database session —
these tests exercise the layer that cannot be.
"""
from __future__ import annotations

import json

import psycopg
import pytest

from .conftest import CLINIC_A, PATIENT_A, PATIENT_B

pytestmark = pytest.mark.db

NOTE = {"subjective": "cough for three days", "objective": "chest clear",
        "assessment": "Viral URTI", "plan": "Symptomatic treatment"}
FACTS = [
    {"fact_type": "diagnosis", "value": "Viral URTI"},
    {"fact_type": "symptom", "value": "cough"},
    {"fact_type": "medication", "value": "paracetamol", "structured": {"context": "prescribed"}},
]

SQL = """
select public.finalize_visit(
  p_patient_id := %s, p_note := %s::jsonb, p_facts := %s::jsonb,
  p_visit_id := %s, p_expected_version := %s, p_draft := %s,
  p_attested := %s, p_consent_given := %s, p_consent_method := %s)
"""


def finalize(session, *, patient=PATIENT_A, note=None, facts=None, visit_id=None,
             expected_version=None, draft=False, attested=True,
             consent=True, method="verbal"):
    return session.one(SQL, (
        patient, json.dumps(note or NOTE), json.dumps(facts if facts is not None else FACTS),
        visit_id, expected_version, draft, attested, consent, method,
    ))


def finalize_expecting_error(session, **kwargs):
    params = {"patient": PATIENT_A, "note": None, "facts": None, "visit_id": None,
              "expected_version": None, "draft": False, "attested": True,
              "consent": True, "method": "verbal"}
    params.update(kwargs)
    return session.expect_error(SQL, (
        params["patient"], json.dumps(params["note"] or NOTE),
        json.dumps(params["facts"] if params["facts"] is not None else FACTS),
        params["visit_id"], params["expected_version"], params["draft"],
        params["attested"], params["consent"], params["method"],
    ))


# --------------------------------------------------------------------- #
# The happy path, and that it is atomic
# --------------------------------------------------------------------- #
def test_signing_writes_visit_note_facts_and_audit_together(doctor_a):
    result = finalize(doctor_a)
    assert result["status"] == "approved"
    assert result["facts_written"] == 3

    visit_id = result["visit_id"]
    assert doctor_a.one("select status from public.visits where id = %s", (visit_id,)) == "approved"
    assert doctor_a.one(
        "select attested from public.soap_notes where visit_id = %s", (visit_id,)) is True
    assert doctor_a.one(
        "select count(*) from public.clinical_facts where visit_id = %s and status = 'confirmed'",
        (visit_id,)) == 3
    assert doctor_a.one(
        "select count(*) from public.audit_log where entity_id = %s and action = 'sign_visit'",
        (visit_id,)) == 1


def test_the_note_content_is_stored_verbatim(doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    row = doctor_a.all(
        "select subjective, objective, assessment, plan from public.soap_notes where visit_id = %s",
        (visit_id,))[0]
    assert row == (NOTE["subjective"], NOTE["objective"], NOTE["assessment"], NOTE["plan"])


def test_attestation_records_who_signed_and_when(doctor_a):
    from .conftest import DOCTOR_A

    visit_id = finalize(doctor_a)["visit_id"]
    attested_by, attested_at = doctor_a.all(
        "select attested_by, attested_at from public.soap_notes where visit_id = %s",
        (visit_id,))[0]
    assert str(attested_by) == DOCTOR_A
    assert attested_at is not None


def test_consent_is_recorded_on_the_visit(doctor_a):
    visit_id = finalize(doctor_a, consent=True, method="verbal")["visit_id"]
    given, method = doctor_a.all(
        "select consent_given, consent_method from public.visits where id = %s", (visit_id,))[0]
    assert given is True
    assert method == "verbal"


def test_a_failure_part_way_through_writes_nothing(doctor_a):
    """The reason this is one function rather than five REST calls.

    An invalid fact_type fails the CHECK constraint after the visit and note
    have already been inserted. In the old five-call flow that left an approved
    visit with no facts and no audit entry; here the whole thing rolls back.
    """
    before_visits = doctor_a.one("select count(*) from public.visits")
    before_notes = doctor_a.one("select count(*) from public.soap_notes")

    error = finalize_expecting_error(
        doctor_a, facts=[{"fact_type": "not_a_valid_type", "value": "x"}])
    assert isinstance(error, psycopg.errors.CheckViolation)

    assert doctor_a.one("select count(*) from public.visits") == before_visits
    assert doctor_a.one("select count(*) from public.soap_notes") == before_notes


# --------------------------------------------------------------------- #
# Attestation cannot be bypassed
# --------------------------------------------------------------------- #
def test_approving_without_attestation_is_refused_by_the_database(doctor_a):
    """Not just by the API. Anyone with a database session hits the same wall."""
    error = finalize_expecting_error(doctor_a, attested=False)
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "attestation is required" in str(error).lower()


def test_a_draft_needs_no_attestation_and_writes_no_facts(doctor_a):
    """An unsigned note must never become longitudinal memory: an abandoned
    consultation would otherwise enter the patient's permanent history."""
    result = finalize(doctor_a, draft=True, attested=False)
    assert result["status"] == "in_progress"
    assert result["facts_written"] == 0
    assert doctor_a.one(
        "select attested from public.soap_notes where visit_id = %s", (result["visit_id"],)) is False
    assert doctor_a.one(
        "select count(*) from public.clinical_facts where visit_id = %s", (result["visit_id"],)) == 0


def test_a_draft_has_no_approved_at_timestamp(doctor_a):
    result = finalize(doctor_a, draft=True, attested=False)
    assert doctor_a.one(
        "select approved_at from public.visits where id = %s", (result["visit_id"],)) is None


# --------------------------------------------------------------------- #
# Role
# --------------------------------------------------------------------- #
def test_staff_cannot_sign_a_note(staff_a):
    error = finalize_expecting_error(staff_a)
    assert isinstance(error, psycopg.errors.InsufficientPrivilege)
    assert "only a doctor may sign" in str(error).lower()


def test_staff_can_save_a_draft(staff_a):
    """Front-desk staff prepare consultations; blocking that would break the
    workflow the role exists to support."""
    result = finalize(staff_a, draft=True, attested=False)
    assert result["status"] == "in_progress"


# --------------------------------------------------------------------- #
# Tenant isolation inside the function
# --------------------------------------------------------------------- #
def test_a_doctor_cannot_finalise_a_visit_for_another_clinics_patient(doctor_a):
    """The function is SECURITY INVOKER precisely so RLS still applies. A
    SECURITY DEFINER version would be a tenant-isolation bypass."""
    error = finalize_expecting_error(doctor_a, patient=PATIENT_B)
    assert isinstance(error, psycopg.errors.NoDataFound)


def test_the_clinic_comes_from_the_jwt_not_the_arguments(doctor_a):
    """There is no clinic parameter at all — the only source is the caller."""
    visit_id = finalize(doctor_a)["visit_id"]
    assert str(doctor_a.one("select clinic_id from public.visits where id = %s", (visit_id,))) == \
        CLINIC_A


# --------------------------------------------------------------------- #
# Immutability of a signed record
# --------------------------------------------------------------------- #
def test_re_finalising_a_signed_visit_is_refused(doctor_a):
    """Last-write-wins on a signed clinical record silently rewrites what the
    physician attested."""
    visit_id = finalize(doctor_a)["visit_id"]
    error = finalize_expecting_error(doctor_a, visit_id=visit_id)
    assert isinstance(error, psycopg.errors.CheckViolation)
    assert "already signed" in str(error).lower()


def test_a_draft_can_be_updated_then_signed(doctor_a):
    draft = finalize(doctor_a, draft=True, attested=False)
    signed = finalize(doctor_a, visit_id=draft["visit_id"],
                      expected_version=draft["version"])
    assert signed["visit_id"] == draft["visit_id"]
    assert signed["status"] == "approved"
    assert signed["facts_written"] == 3
    # One note per visit, not two.
    assert doctor_a.one(
        "select count(*) from public.soap_notes where visit_id = %s", (draft["visit_id"],)) == 1


# --------------------------------------------------------------------- #
# Optimistic locking
# --------------------------------------------------------------------- #
def test_a_stale_version_is_rejected(doctor_a):
    """Two clinicians finalising the same consultation used to be
    last-write-wins, with no signal to either of them."""
    draft = finalize(doctor_a, draft=True, attested=False)
    stale_version = draft["version"]

    # Someone else edits the draft in between.
    finalize(doctor_a, visit_id=draft["visit_id"], draft=True, attested=False,
             note={**NOTE, "assessment": "Someone else's edit"})

    error = finalize_expecting_error(doctor_a, visit_id=draft["visit_id"],
                                     expected_version=stale_version)
    assert isinstance(error, psycopg.errors.SerializationFailure)
    assert "modified by someone else" in str(error).lower()


def test_the_current_version_is_accepted(doctor_a):
    draft = finalize(doctor_a, draft=True, attested=False)
    current = doctor_a.one("select version from public.visits where id = %s", (draft["visit_id"],))
    assert finalize(doctor_a, visit_id=draft["visit_id"], expected_version=current)["status"] == \
        "approved"


def test_omitting_the_version_skips_the_check(doctor_a):
    """Backwards compatible for clients that have not adopted the lock yet."""
    draft = finalize(doctor_a, draft=True, attested=False)
    assert finalize(doctor_a, visit_id=draft["visit_id"], expected_version=None)["status"] == \
        "approved"


def test_version_advances_on_every_write(doctor_a):
    draft = finalize(doctor_a, draft=True, attested=False)
    v1 = doctor_a.one("select version from public.visits where id = %s", (draft["visit_id"],))
    finalize(doctor_a, visit_id=draft["visit_id"], draft=True, attested=False)
    v2 = doctor_a.one("select version from public.visits where id = %s", (draft["visit_id"],))
    assert v2 > v1


# --------------------------------------------------------------------- #
# Facts: supersession rather than mutation
# --------------------------------------------------------------------- #
def test_facts_carry_provenance_and_temporal_status(doctor_a):
    visit_id = finalize(doctor_a)["visit_id"]
    rows = doctor_a.all(
        "select fact_type, source, status, clinical_status, asserted_by, asserted_at "
        "from public.clinical_facts where visit_id = %s", (visit_id,))
    assert len(rows) == 3
    for _, source, status, clinical_status, asserted_by, asserted_at in rows:
        assert source == "doctor_confirmed_ai"
        assert status == "confirmed"
        assert clinical_status == "current"
        assert asserted_by is not None
        assert asserted_at is not None


def test_empty_fact_values_are_skipped(doctor_a):
    result = finalize(doctor_a, facts=[
        {"fact_type": "symptom", "value": ""},
        {"fact_type": "symptom", "value": "   "},
        {"fact_type": "symptom", "value": "cough"},
    ])
    assert result["facts_written"] == 1


def test_long_fact_values_are_truncated_not_rejected(doctor_a):
    result = finalize(doctor_a, facts=[{"fact_type": "diagnosis", "value": "x" * 900}])
    assert result["facts_written"] == 1
    visit_id = result["visit_id"]
    assert doctor_a.one(
        "select length(value) from public.clinical_facts where visit_id = %s", (visit_id,)) == 500
