"""Endpoint behaviour: upload limits, extraction grounding, search, degradation.

These run the full request path with only the third parties mocked.
"""
from __future__ import annotations

import json

import httpx
import pytest
import respx

from .conftest import PATIENT_A, SUPABASE

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/"
OPENAI_STT = "https://api.openai.com/v1/audio/transcriptions"
PATIENTS_URL = f"{SUPABASE}/rest/v1/patients"
FACTS_URL = f"{SUPABASE}/rest/v1/clinical_facts"

TRANSCRIPT = (
    "Doctor: kya problem hai? Patient: mujhe chest pain hai since two days, "
    "aur sweating ho rahi hai. Doctor: koi allergy? Patient: nahi, no known allergies."
)


def _gemini(payload: dict):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}],
        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 25},
    })


# ===================================================================== #
# Speech-to-text upload handling
# ===================================================================== #
def test_transcribe_without_a_provider_is_503(as_doctor, settings_env):
    settings_env(OPENAI_API_KEY="", SARVAM_API_KEY="")
    r = as_doctor.post("/scribe/transcribe",
                       files={"file": ("a.wav", b"RIFF0000", "audio/wav")})
    assert r.status_code == 503
    assert "speech-to-text" in r.json()["detail"].lower()


def test_oversized_audio_is_rejected(as_doctor, settings_env):
    """`await file.read()` with no ceiling let one request pull an arbitrary
    payload into memory before any check ran."""
    settings_env(OPENAI_API_KEY="k", MAX_AUDIO_BYTES=str(1024))
    r = as_doctor.post("/scribe/transcribe",
                       files={"file": ("big.wav", b"\0" * 5000, "audio/wav")})
    assert r.status_code == 413


def test_unsupported_content_type_is_rejected_before_reaching_a_paid_api(
        as_doctor, settings_env):
    settings_env(OPENAI_API_KEY="k")
    with respx.mock:
        upstream = respx.post(OPENAI_STT).mock(return_value=httpx.Response(200, json={"text": "x"}))
        r = as_doctor.post("/scribe/transcribe",
                           files={"file": ("payload.exe", b"MZ\x90", "application/x-msdownload")})
    assert r.status_code == 415
    assert upstream.call_count == 0


def test_empty_audio_is_rejected(as_doctor, settings_env):
    settings_env(OPENAI_API_KEY="k")
    r = as_doctor.post("/scribe/transcribe", files={"file": ("a.wav", b"", "audio/wav")})
    assert r.status_code == 400


@respx.mock
def test_stt_failure_is_reported_without_losing_the_consultation(as_doctor, settings_env):
    settings_env(OPENAI_API_KEY="k", SARVAM_API_KEY="")
    respx.post(OPENAI_STT).mock(return_value=httpx.Response(500))
    r = as_doctor.post("/scribe/transcribe",
                       files={"file": ("a.wav", b"RIFF0000", "audio/wav")})
    assert r.status_code == 502
    assert "not lost" in r.json()["detail"]


# ===================================================================== #
# Extraction and grounding
# ===================================================================== #
@respx.mock
def test_extraction_keeps_real_quotes_and_drops_invented_ones(as_doctor, settings_env):
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=_gemini({
        "chief_complaints": [
            {"text": "chest pain", "duration": "2 days", "evidence": "mujhe chest pain hai"},
            {"text": "headache", "duration": "", "evidence": "patient reports severe headache"},
        ],
        "hpi": "Chest pain for two days with sweating.",
        "allergies": "No known drug allergies",
        "evidence": {
            "hpi": "sweating ho rahi hai",
            "allergies": "the patient is allergic to penicillin",
        },
    }))

    r = as_doctor.post("/scribe/extract", json={"transcript": TRANSCRIPT})
    assert r.status_code == 200
    body = r.json()

    complaints = {c["text"]: c["evidence"] for c in body["chief_complaints"]}
    assert complaints["chest pain"] == "mujhe chest pain hai"
    assert complaints["headache"] == "", "an invented quote must be dropped"

    assert body["evidence"]["hpi"] == "sweating ho rahi hai"
    assert "allergies" not in body["evidence"], "an invented allergy quote must be dropped"


@respx.mock
def test_extraction_reports_evidence_coverage(as_doctor, settings_env):
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=_gemini({
        "hpi": "Chest pain for two days.",
        "medications": "none",
        "evidence": {"hpi": "mujhe chest pain hai"},
    }))
    body = as_doctor.post("/scribe/extract", json={"transcript": TRANSCRIPT}).json()
    grounding = body["grounding"]
    assert grounding["fields_populated"] == 2
    assert grounding["fields_evidenced"] == 1
    assert grounding["evidence_coverage_pct"] == 50
    assert "medications" in grounding["unevidenced_fields"]


@respx.mock
def test_extraction_includes_a_deterministic_completeness_score(as_doctor, settings_env):
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=_gemini({
        "chief_complaints": [{"text": "chest pain", "duration": "2 days"}],
        "hpi": "Chest pain for two days with sweating.",
        "allergies": "No known drug allergies",
    }))
    body = as_doctor.post("/scribe/extract", json={"transcript": TRANSCRIPT}).json()
    assert body["completeness"]["metric"] == "documentation_completeness"
    assert "assessment" in body["completeness"]["missing_required"]


@respx.mock
def test_extraction_degrades_to_empty_when_every_provider_fails(as_doctor, settings_env):
    """A failed extraction must not 500 the consultation."""
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=httpx.Response(503))
    r = as_doctor.post("/scribe/extract", json={"transcript": TRANSCRIPT})
    assert r.status_code == 200
    assert r.json()["available"] is False
    assert r.json()["chief_complaints"] == []


def test_short_transcript_short_circuits_without_calling_a_provider(as_doctor, settings_env):
    settings_env(GEMINI_API_KEY="k")
    with respx.mock:
        upstream = respx.post(url__startswith=GEMINI)
        r = as_doctor.post("/scribe/extract", json={"transcript": "  "})
    assert r.status_code == 200
    assert upstream.call_count == 0


def test_oversized_transcript_is_rejected_by_validation(as_doctor):
    r = as_doctor.post("/scribe/extract", json={"transcript": "x" * 300_000})
    assert r.status_code == 422


# ===================================================================== #
# The live lane fails open
# ===================================================================== #
@respx.mock
def test_live_lane_fails_open_and_says_so(as_doctor, settings_env):
    """Losing live suggestions must never interrupt a consultation, but the
    client has to know they are missing rather than reading an empty panel as
    'nothing to worry about'."""
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=httpx.Response(503))
    r = as_doctor.post("/scribe/live", json={"transcript": TRANSCRIPT})
    assert r.status_code == 200
    assert r.json()["available"] is False


@respx.mock
def test_live_lane_normalises_malformed_red_flags(as_doctor, settings_env):
    """Model output is untrusted input: the UI must not have to handle a red
    flag that is a string, or one with a null urgency."""
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="")
    respx.post(url__startswith=GEMINI).mock(return_value=_gemini({
        "translation": "Patient reports chest pain.",
        "symptoms": ["chest pain", "", None],
        "red_flags": [
            "just a string",
            {"no_finding_key": "x"},
            {"finding": "chest pain", "urgency": "CATASTROPHIC", "concern": "ACS", "action": "ECG"},
        ],
        "questions": [{"question": "Ask about radiation", "severity": "HIGH"}, "junk"],
    }))
    body = as_doctor.post("/scribe/live", json={"transcript": TRANSCRIPT}).json()
    assert body["symptoms"] == ["chest pain"]
    assert len(body["red_flags"]) == 1
    assert body["red_flags"][0]["urgency"] == "routine", "unknown urgency must fall back safely"
    assert body["red_flags"][0]["source"] == "ai"
    assert len(body["questions"]) == 1


# ===================================================================== #
# Patient search and pagination
# ===================================================================== #
@respx.mock
def test_patient_search_sends_a_quoted_filter(as_doctor):
    captured = {}

    def handler(request):
        captured["or"] = request.url.params.get("or")
        return httpx.Response(200, json=[], headers={"content-range": "0-0/0"})

    respx.get(PATIENTS_URL).mock(side_effect=handler)
    payload = "a),name.eq.x,("
    as_doctor.get("/patients", params={"q": payload})
    expected = ",".join(f'{col}.ilike."*{payload}*"' for col in ("name", "phone", "uhid"))
    assert captured["or"] == f"({expected})"


@respx.mock
def test_patient_total_is_the_matching_count_not_the_page_size(as_doctor):
    """`total = len(items)` made the number meaningless once pagination existed."""
    respx.get(PATIENTS_URL).mock(return_value=httpx.Response(
        200, json=[{"id": PATIENT_A, "name": "Asha"}], headers={"content-range": "0-0/431"}))
    body = as_doctor.get("/patients", params={"limit": 1}).json()
    assert body["total"] == 431
    assert len(body["items"]) == 1
    assert body["limit"] == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 5000}, {"offset": -1},
                                    {"q": "x" * 500}])
def test_pagination_parameters_are_validated(as_doctor, params):
    assert as_doctor.get("/patients", params=params).status_code == 422


@respx.mock
def test_creating_a_patient_never_trusts_a_client_supplied_clinic(as_doctor):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.read()))
        return httpx.Response(201, json=[{"id": PATIENT_A, "name": "Asha", "uhid": "CH-2026-000001"}])

    respx.get(PATIENTS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.post(PATIENTS_URL).mock(side_effect=handler)
    respx.post(f"{SUPABASE}/rest/v1/audit_log").mock(return_value=httpx.Response(201, json=[]))

    from .conftest import CLINIC_A
    as_doctor.post("/patients", json={"name": "Asha", "clinic_id": "22222222-2222-2222-2222-222222222222"})
    assert captured["clinic_id"] == CLINIC_A


def test_future_date_of_birth_is_rejected(as_doctor):
    """A future DOB silently produces a negative age that flows into the risk
    model's age features as a valid number."""
    r = as_doctor.post("/patients", json={"name": "Asha", "dob": "2099-01-01"})
    assert r.status_code == 422


@respx.mock
def test_audit_records_which_fields_changed_not_their_values(as_doctor):
    """The audit log is queried and exported; duplicating demographics into it
    widens the blast radius of any access to it."""
    captured = {}

    def audit_handler(request):
        captured.update(json.loads(request.read()))
        return httpx.Response(201, json=[])

    respx.post(PATIENTS_URL).mock(return_value=httpx.Response(
        201, json=[{"id": PATIENT_A, "name": "Asha Rao", "uhid": "CH-2026-000001"}]))
    respx.post(f"{SUPABASE}/rest/v1/audit_log").mock(side_effect=audit_handler)

    as_doctor.post("/patients", json={"name": "Asha Rao", "phone": "9000000001"})
    assert "Asha Rao" not in json.dumps(captured["after"])
    assert "9000000001" not in json.dumps(captured["after"])
    assert set(captured["after"]["fields"]) == {"name", "phone", "clinic_id"}


# ===================================================================== #
# Longitudinal memory endpoints
# ===================================================================== #
@respx.mock
def test_memory_distinguishes_documented_none_from_never_asked(as_doctor):
    """'No known drug allergies' is an answer; blank is not. Showing them the
    same way is how an allergy check silently means nothing."""
    respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=[
        {"fact_type": "allergy", "value": "No known drug allergies", "visit_id": "v1",
         "asserted_at": "2026-01-01T10:00:00+00:00", "status": "confirmed",
         "clinical_status": "current", "structured": {"documented_negative": True}},
    ]))
    body = as_doctor.get(f"/patients/{PATIENT_A}/memory").json()
    assert body["allergy_status"] == "documented_none"
    assert body["allergies"] == []


@respx.mock
def test_memory_with_no_facts_reports_not_recorded(as_doctor):
    respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=[]))
    body = as_doctor.get(f"/patients/{PATIENT_A}/memory").json()
    assert body["allergy_status"] == "not_recorded"
    assert body["visit_count"] == 0


@respx.mock
def test_memory_only_uses_confirmed_facts(as_doctor):
    """A fact a physician never signed is not memory."""
    captured = {}

    def handler(request):
        captured["status"] = request.url.params.get("status")
        return httpx.Response(200, json=[])

    respx.get(FACTS_URL).mock(side_effect=handler)
    as_doctor.get(f"/patients/{PATIENT_A}/memory")
    assert captured["status"] == "eq.confirmed"


@respx.mock
def test_analytics_states_its_method(as_doctor):
    respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=[
        {"fact_type": "vital", "value": "bp: 130/84", "visit_id": f"v{i}",
         "asserted_at": f"2026-0{i}-01T10:00:00+00:00", "status": "confirmed",
         "clinical_status": "current",
         "structured": {"metric": "bp", "reading": f"{120 + i * 8}/8{i}"}}
        for i in range(1, 6)
    ]))
    body = as_doctor.get(f"/patients/{PATIENT_A}/analytics").json()
    assert body["available"] is True
    assert "Mann-Kendall" in body["method"]["trend_test"]
    assert body["trends"]["bp"]["direction"] == "rising"
    assert "bp" in body["flagged_metrics"]


@respx.mock
def test_analytics_with_no_data_says_so_instead_of_inventing_a_trend(as_doctor):
    respx.get(FACTS_URL).mock(return_value=httpx.Response(200, json=[]))
    body = as_doctor.get(f"/patients/{PATIENT_A}/analytics").json()
    assert body["available"] is False
    assert body["fact_count"] == 0


# ===================================================================== #
# Readiness
# ===================================================================== #
@respx.mock
def test_readiness_reports_dependency_state(client, settings_env):
    settings_env(GEMINI_API_KEY="k", OPENAI_API_KEY="", SARVAM_API_KEY="s")
    respx.get(f"{SUPABASE}/rest/v1/").mock(return_value=httpx.Response(200, json={}))
    body = client.get("/health/ready").json()
    assert body["ready"] is True
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["stt"]["chain"] == ["sarvam"]
    assert body["checks"]["llm"]["chain"] == ["gemini/gemini-2.5-flash",
                                              "gemini/gemini-2.0-flash",
                                              "gemini/gemini-2.5-flash-lite"]


@respx.mock
def test_readiness_is_503_when_the_database_is_unreachable(client):
    respx.get(f"{SUPABASE}/rest/v1/").mock(side_effect=httpx.ConnectError("down"))
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["ready"] is False


@respx.mock
def test_a_missing_ai_provider_does_not_make_the_server_unready(client, settings_env):
    """Marking the server unready would take documentation offline to protect a
    convenience feature."""
    settings_env(GEMINI_API_KEY="", OPENAI_API_KEY="", SARVAM_API_KEY="")
    respx.get(f"{SUPABASE}/rest/v1/").mock(return_value=httpx.Response(200, json={}))
    body = client.get("/health/ready").json()
    assert body["ready"] is True
    assert body["checks"]["llm"]["ok"] is False


def test_liveness_never_touches_a_dependency(client):
    """No respx mock is installed: if /health made a network call it would fail."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
