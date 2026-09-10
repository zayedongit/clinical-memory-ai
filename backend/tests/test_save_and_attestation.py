"""The save path: attestation, role, optimistic locking, and error translation.

The database enforces all of these too (see `tests/db/test_finalize_visit.py`).
These tests cover the API half of that defence in depth: the caller gets a
status code and a message they can act on, and an unattested note never reaches
the database at all.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.api.routers.scribe import build_facts

from .conftest import PATIENT_A, SUPABASE

RPC_FINALIZE = f"{SUPABASE}/rest/v1/rpc/finalize_visit"


def _minimal_save(**overrides) -> dict:
    body = {
        "patient_id": PATIENT_A,
        "status": "completed",
        "attested": True,
        "soap": {"subjective": "cough", "assessment": "URTI", "plan": "rest"},
        "entities": {"symptoms": ["cough"], "diagnoses": ["URTI"]},
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------- #
# Attestation is fail-closed
# --------------------------------------------------------------------- #
@respx.mock
def test_completing_without_attestation_is_rejected_before_any_write(as_doctor):
    route = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(200, json={}))
    r = as_doctor.post("/scribe/save", json=_minimal_save(attested=False))
    assert r.status_code == 400
    assert "attestation" in r.json()["detail"].lower()
    assert route.call_count == 0, "an unattested note must never reach the database"


@respx.mock
def test_a_draft_does_not_require_attestation(as_doctor):
    route = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        200, json={"visit_id": "v1", "patient_id": PATIENT_A, "status": "in_progress",
                   "version": 2, "facts_written": 0}))
    r = as_doctor.post("/scribe/save", json=_minimal_save(status="in_progress", attested=False))
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"
    sent = route.calls[0].request.read().decode()
    assert '"p_draft":true' in sent.replace(" ", "")


@respx.mock
def test_draft_writes_no_clinical_facts(as_doctor):
    """An unsigned note must not become longitudinal memory.

    If a draft contributed confirmed facts, an abandoned consultation would
    silently enter the patient's permanent history.
    """
    route = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        200, json={"visit_id": "v1", "patient_id": PATIENT_A, "status": "in_progress",
                   "version": 2, "facts_written": 0}))
    as_doctor.post("/scribe/save", json=_minimal_save(status="in_progress", attested=False))
    import json as _json
    sent = _json.loads(route.calls[0].request.read())
    assert sent["p_facts"] == []


# --------------------------------------------------------------------- #
# Role
# --------------------------------------------------------------------- #
@respx.mock
def test_staff_cannot_sign_a_note(as_staff):
    route = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(200, json={}))
    r = as_staff.post("/scribe/save", json=_minimal_save())
    assert r.status_code == 403
    assert "staff" in r.json()["detail"]
    assert route.call_count == 0


@respx.mock
def test_staff_may_still_save_a_draft(as_staff):
    """Clinic staff prepare consultations; only a doctor signs one.

    Blocking staff from drafting would break the actual clinic workflow, which
    is the reason the role check is on attestation rather than on writing.
    """
    respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        200, json={"visit_id": "v1", "patient_id": PATIENT_A, "status": "in_progress",
                   "version": 2, "facts_written": 0}))
    r = as_staff.post("/scribe/save", json=_minimal_save(status="in_progress", attested=False))
    assert r.status_code == 200


@respx.mock
def test_staff_cannot_amend_or_remove_a_record(as_staff):
    r = as_staff.post("/visits/11111111-1111-1111-1111-111111111111/amend",
                      json={"reason": "typo", "text": "corrected"})
    assert r.status_code == 403
    r = as_staff.post("/visits/11111111-1111-1111-1111-111111111111/delete",
                      json={"reason": "duplicate entry"})
    assert r.status_code == 403


# --------------------------------------------------------------------- #
# Database error translation
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(("sqlstate", "expected"), [
    ("40001", 409),   # serialization_failure — someone else finalised first
    ("23514", 409),   # check_violation — already signed
    ("42501", 403),   # insufficient_privilege — wrong role
    ("02000", 404),   # no_data_found — patient/visit not visible
    ("P0001", 400),   # plain raise_exception
])
@respx.mock
def test_database_rule_violations_map_to_actionable_status_codes(as_doctor, sqlstate, expected):
    """Without this mapping every rule violation is an opaque 502 and the UI
    cannot tell 'someone else edited this' from 'the database is down'."""
    respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        400, json={"code": sqlstate, "message": "rule violated"}))
    r = as_doctor.post("/scribe/save", json=_minimal_save())
    assert r.status_code == expected


@respx.mock
def test_unknown_database_error_does_not_leak_internals(as_doctor):
    respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        500, text='relation "patients" does not exist at character 41'))
    r = as_doctor.post("/scribe/save", json=_minimal_save())
    assert r.status_code == 502
    assert "relation" not in r.json()["detail"]
    assert "nothing was written" in r.json()["detail"].lower()


@respx.mock
def test_optimistic_lock_version_is_forwarded(as_doctor):
    route = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        200, json={"visit_id": "v1", "patient_id": PATIENT_A, "status": "approved",
                   "version": 4, "facts_written": 2}))
    as_doctor.post("/scribe/save", json=_minimal_save(visit_id="v1", expected_version=3))
    import json as _json
    sent = _json.loads(route.calls[0].request.read())
    assert sent["p_expected_version"] == 3
    assert sent["p_visit_id"] == "v1"


# --------------------------------------------------------------------- #
# Fact construction — what becomes longitudinal memory
# --------------------------------------------------------------------- #
def test_reported_and_prescribed_medications_are_distinguished():
    """Conflating them makes the medication timeline claim a drug was stopped
    that was never started."""
    facts = build_facts(
        entities={"medications": ["metformin 500mg"]},
        prescription=[{"generic": "amoxicillin", "dose": "500mg"}],
        vitals={},
    )
    contexts = {f["value"]: f["structured"]["context"] for f in facts if f["fact_type"] == "medication"}
    assert contexts["metformin 500mg"] == "reported"
    assert contexts["amoxicillin"] == "prescribed"


def test_no_known_allergies_is_recorded_as_a_documented_negative():
    """'No known drug allergies' is an answer, not an allergen.

    Storing the sentence as an allergy value makes every substring-based
    allergy check fire against unrelated drugs.
    """
    facts = build_facts(entities={"allergies": ["No known drug allergies"]},
                        prescription=[], vitals={})
    allergies = [f for f in facts if f["fact_type"] == "allergy"]
    assert len(allergies) == 1
    assert allergies[0]["structured"]["documented_negative"] is True


def test_multiple_allergens_are_split_into_separate_facts():
    facts = build_facts(entities={"allergies": ["penicillin, sulfa drugs"]},
                        prescription=[], vitals={})
    values = sorted(f["value"] for f in facts if f["fact_type"] == "allergy")
    assert values == ["penicillin", "sulfa drugs"]


def test_blank_and_whitespace_values_are_dropped():
    facts = build_facts(entities={"symptoms": ["", "   ", "cough"]},
                        prescription=[{"generic": ""}], vitals={"bp": "  "})
    assert [f["value"] for f in facts] == ["cough"]


def test_vitals_become_typed_facts_with_metric_and_reading():
    facts = build_facts(entities={}, prescription=[], vitals={"bp": "138/86", "spo2": "97"})
    by_metric = {f["structured"]["metric"]: f["structured"]["reading"]
                 for f in facts if f["fact_type"] == "vital"}
    assert by_metric == {"bp": "138/86", "spo2": "97"}


def test_fact_values_are_length_capped():
    facts = build_facts(entities={"diagnoses": ["x" * 900]}, prescription=[], vitals={})
    assert len(facts[0]["value"]) == 500


def test_every_fact_carries_provenance_and_temporal_status():
    facts = build_facts(
        entities={"diagnoses": ["Type 2 diabetes"], "symptoms": ["polyuria"]},
        prescription=[{"generic": "metformin"}], vitals={"hr": "78"},
    )
    assert facts, "expected facts"
    for f in facts:
        assert f["source"] == "doctor_confirmed_ai"
        assert f["clinical_status"] in ("current", "historical", "resolved", "unknown")


# --------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------- #
def test_save_requires_a_patient(as_doctor):
    r = as_doctor.post("/scribe/save", json={"status": "completed", "attested": True})
    assert r.status_code == 400
    assert "patient" in r.json()["detail"].lower()


# --------------------------------------------------------------------- #
# Patient creation is part of the same transaction
# --------------------------------------------------------------------- #
@respx.mock
def test_a_new_patient_is_sent_to_the_database_function_not_created_beforehand(as_doctor):
    """It used to be a separate PostgREST insert before the RPC, so a failed
    save left the patient committed — and every retry made another record for
    the same person."""
    patients = respx.post(f"{SUPABASE}/rest/v1/patients")
    rpc = respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        200, json={"visit_id": "v1", "patient_id": "p-new", "status": "approved",
                   "version": 1, "facts_written": 2, "patient_created": True}))

    body = _minimal_save()
    body.pop("patient_id")
    body["new_patient"] = {"name": "Walk-in Patient", "age": 34, "gender": "female"}
    r = as_doctor.post("/scribe/save", json=body)

    assert r.status_code == 200
    assert patients.call_count == 0, "the patient must not be created outside the transaction"

    import json as _json
    sent = _json.loads(rpc.calls[0].request.read())
    assert sent["p_patient_id"] is None
    assert sent["p_new_patient"]["name"] == "Walk-in Patient"
    assert sent["p_new_patient"]["dob"].endswith("-01-01"), "an age becomes a birth year"


@respx.mock
def test_a_failed_save_with_a_new_patient_creates_nothing(as_doctor):
    patients = respx.post(f"{SUPABASE}/rest/v1/patients")
    respx.post(RPC_FINALIZE).mock(return_value=httpx.Response(
        400, json={"code": "40001", "message": "modified by someone else"}))

    body = _minimal_save()
    body.pop("patient_id")
    body["new_patient"] = {"name": "Walk-in Patient"}
    r = as_doctor.post("/scribe/save", json=body)

    assert r.status_code == 409
    assert patients.call_count == 0


def test_a_save_with_neither_a_patient_nor_a_new_patient_is_rejected(as_doctor):
    body = _minimal_save()
    body.pop("patient_id")
    r = as_doctor.post("/scribe/save", json=body)
    assert r.status_code == 400
    assert "patient" in r.json()["detail"].lower()
