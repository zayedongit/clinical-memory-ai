"""The AI scribe: audio in, a physician-reviewable draft out.

    POST /scribe/transcribe  audio (multipart) -> transcript          (STT chain)
    POST /scribe/live        running transcript -> live assistance    (LLM chain)
    POST /scribe/extract     transcript -> structured encounter + verified evidence
    POST /scribe/soap        transcript -> full SOAP note
    POST /scribe/save        the reviewed encounter -> one atomic database write

Nothing here writes a clinical fact on its own. `/scribe/save` is the only
route that touches the record, and it delegates to `finalize_visit()` in the
database, which enforces attestation, role and optimistic locking in a single
transaction — so the guarantees hold even if this code is wrong.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from ...ai import citations, prompts, providers, stt
from ...clinical import completeness, risk
from ...core.budget import budget
from ...core.config import get_settings
from ...core.metrics import registry
from ...core.supabase import rpc, user_headers
from ..deps import CurrentUser, get_current_user

log = logging.getLogger("scribe")
router = APIRouter(prefix="/scribe")

_URGENCY = {"emergency", "urgent", "routine"}
_URG_RANK = {"emergency": 0, "urgent": 1, "routine": 2}
_SEV_RANK = {"high": 0, "moderate": 1, "low": 2}

# Audio container types the providers accept. Anything else is rejected before
# it reaches a paid API, both to save spend and because an unbounded
# content-type is an easy way to probe an upstream.
_AUDIO_TYPES = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/webm", "audio/ogg",
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a", "audio/x-m4a",
    "audio/flac", "video/webm",
}


def _budget_guard() -> None:
    if budget.exhausted():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "The daily AI budget for this instance is exhausted. Documentation still works; "
            "AI assistance resumes tomorrow or when the budget is raised.",
        )


# --------------------------------------------------------------------- #
# Speech to text
# --------------------------------------------------------------------- #
@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not stt.providers():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No speech-to-text provider configured (set OPENAI_API_KEY or SARVAM_API_KEY).",
        )
    _budget_guard()

    ctype = (file.content_type or "audio/wav").split(";")[0].strip().lower()
    if ctype not in _AUDIO_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            f"Unsupported audio type '{ctype}'.")

    # Read with a ceiling rather than `await file.read()`. The unbounded read
    # let one request pull an arbitrary payload fully into memory before any
    # check ran, which is a trivial denial of service on a single-instance API.
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1 << 20):
        total += len(chunk)
        if total > s.max_audio_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"Audio exceeds the {s.max_audio_bytes // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)
    audio = b"".join(chunks)
    if not audio:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty audio upload.")

    try:
        result = await stt.transcribe(audio, filename=file.filename or "audio.wav", content_type=ctype)
    except stt.AllSTTFailed as e:
        log.error("stt_failed", extra={"extra_fields": {"attempts": e.attempts}})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Speech-to-text is unavailable right now. Your recording was not lost — "
            "press record again, or type the note manually.",
        ) from None

    budget.record(result.cost_usd)
    return {
        "transcript": result.text,
        "language": result.language,
        "provider": result.provider,
        "fell_back": result.fell_back,
    }


# --------------------------------------------------------------------- #
# Live assistance
# --------------------------------------------------------------------- #
class LiveRequest(BaseModel):
    transcript: str = Field(max_length=200_000)
    patient_context: str | None = Field(default=None, max_length=8_000)


@router.post("/live")
async def live(body: LiveRequest, user: CurrentUser = Depends(get_current_user)):
    if len(body.transcript.strip()) < 3:
        return {"translation": "", "symptoms": [], "red_flags": [], "questions": []}
    _budget_guard()

    try:
        result = await providers.generate_json(
            prompts.live(body.patient_context or "", _cap(body.transcript)),
            capability="live", max_tokens=2048, timeout=30.0,
        )
    except providers.AllProvidersFailed:
        # Fail open. Losing live suggestions must never interrupt a
        # consultation in progress; the physician keeps working, and the panel
        # says the assistant is unavailable rather than showing a stale answer.
        return {"translation": "", "symptoms": [], "red_flags": [], "questions": [],
                "available": False}

    budget.record(result.cost_usd)
    data = result.data
    considerations = normalise_considerations({"red_flags": data.get("red_flags")})
    red_flags = []
    for rf in considerations["red_flags"][:3]:
        rf["action"] = (rf.get("action") or "")[:140]
        rf["concern"] = (rf.get("concern") or "")[:120]
        red_flags.append(rf)

    return {
        "available": True,
        "translation": str(data.get("translation", "")).strip(),
        "symptoms": _string_list(data.get("symptoms"))[:10],
        "red_flags": red_flags,
        "questions": [
            {"question": str(q.get("question", "")).strip()[:160],
             "severity": str(q.get("severity", "low")).strip().lower()}
            for q in (data.get("questions") or []) if isinstance(q, dict) and q.get("question")
        ][:4],
        "provider": result.provider,
    }


# --------------------------------------------------------------------- #
# Structured extraction — the lane whose evidence is verified
# --------------------------------------------------------------------- #
class ExtractRequest(BaseModel):
    transcript: str = Field(max_length=200_000)
    patient_context: str | None = Field(default=None, max_length=8_000)


_EVIDENCE_FIELDS = ("hpi", "past_history", "allergies", "medications",
                    "general_exam", "systemic_exam", "vitals")
_VITAL_KEYS = ("bp", "hr", "temp", "spo2", "rr", "weight", "height")


def _empty_extract() -> dict:
    return {"chief_complaints": [], "hpi": "", "past_history": "", "allergies": "",
            "medications": "", "general_exam": "", "systemic_exam": "", "vitals": {},
            "evidence": {}, "grounding": citations.coverage({}, {}), "available": False}


@router.post("/extract")
async def extract(body: ExtractRequest, user: CurrentUser = Depends(get_current_user)):
    if len(body.transcript.strip()) < 3:
        return _empty_extract()
    _budget_guard()

    transcript = _cap(body.transcript)
    try:
        result = await providers.generate_json(
            prompts.extract(body.patient_context or "", transcript),
            capability="extract", max_tokens=4096, timeout=45.0,
        )
    except providers.AllProvidersFailed:
        return _empty_extract()

    budget.record(result.cost_usd)
    data = result.data

    complaints = []
    for c in (data.get("chief_complaints") or []):
        if isinstance(c, dict) and c.get("text"):
            complaints.append({
                "text": str(c["text"]).strip()[:120],
                "duration": str(c.get("duration", "")).strip()[:40],
                "evidence": citations.verify_or_drop(c.get("evidence", ""), transcript)[:200],
            })
        elif isinstance(c, str) and c.strip():
            complaints.append({"text": c.strip()[:120], "duration": "", "evidence": ""})

    raw_vitals = data.get("vitals") if isinstance(data.get("vitals"), dict) else {}
    vitals = {k: str(raw_vitals.get(k, "")).strip() for k in _VITAL_KEYS if str(raw_vitals.get(k, "")).strip()}

    fields = {k: str(data.get(k, "")).strip() for k in _EVIDENCE_FIELDS if k != "vitals"}
    raw_evidence = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
    evidence = {}
    for key in _EVIDENCE_FIELDS:
        verified = citations.verify_or_drop(raw_evidence.get(key, ""), transcript)[:200]
        if verified:
            evidence[key] = verified

    payload = {
        "chief_complaints": complaints[:10],
        **fields,
        "vitals": vitals,
        "evidence": evidence,
        # How much of what the model filled in is actually backed by words the
        # microphone heard. Shown to the physician, and exported as a metric.
        "grounding": citations.coverage(
            {**fields, "vitals": " ".join(vitals.values())}, evidence
        ),
        "available": True,
        "provider": result.provider,
        "repaired_json": result.repaired,
    }
    payload["completeness"] = completeness.score(payload)
    return payload


# --------------------------------------------------------------------- #
# Full SOAP note
# --------------------------------------------------------------------- #
class SoapRequest(BaseModel):
    transcript: str = Field(max_length=200_000)
    patient_context: str | None = Field(default=None, max_length=8_000)
    mode: str = "interim"          # "interim" | "final"


@router.post("/soap")
async def soap(body: SoapRequest, user: CurrentUser = Depends(get_current_user)):
    if len(body.transcript.strip()) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transcript is empty")
    if not providers.candidates():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No LLM provider is configured")
    _budget_guard()

    try:
        result = await providers.generate_json(
            prompts.soap(_cap(body.transcript), body.patient_context or "", body.mode),
            capability="soap", max_tokens=8192, timeout=90.0,
        )
    except providers.AllProvidersFailed as e:
        log.error("soap_failed", extra={"extra_fields": {"attempts": e.attempts}})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The AI could not produce a note this time. Press Analyse again, or write the "
            "note manually — nothing has been lost.",
        ) from None

    budget.record(result.cost_usd)
    data = result.data
    so = data.get("soap") if isinstance(data.get("soap"), dict) else {}
    entities_in = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    entities = {k: _string_list(entities_in.get(k)) for k in
                ("symptoms", "medications", "allergies", "diagnoses", "follow_up")}

    considerations = normalise_considerations(data.get("clinical_considerations"))
    considerations["red_flags"] = _trim_flags(considerations["red_flags"])

    follow_ups = [] if body.mode == "final" else _dedupe_followups(
        [
            {
                "question": str(q.get("question", "")).strip(),
                "concern": str(q.get("concern", "")).strip(),
                "likelihood_pct": _pct(q.get("likelihood_pct")),
                "severity": str(q.get("severity", "low")).lower(),
            }
            for q in (data.get("follow_up_questions") or [])
            if isinstance(q, dict) and q.get("question")
        ],
        considerations["red_flags"],
    )

    note = {
        "dialogue": [d for d in (data.get("dialogue") or []) if isinstance(d, dict) and d.get("text")],
        "soap": {k: str(so.get(k, "") or "") for k in ("subjective", "objective", "assessment", "plan")},
        "entities": entities,
        "follow_up_questions": follow_ups,
        "clinical_considerations": considerations,
        "provider": result.provider,
    }
    # Completeness is computed here, not asked of the model: it must be
    # reproducible and explainable, and the model's own estimate was neither.
    note["completeness"] = completeness.score({**note["soap"], "entities": entities})
    note["clinical_considerations"]["missing_information"] = completeness.missing_information(
        {**note["soap"], "entities": entities}
    )
    return note


# --------------------------------------------------------------------- #
# Escalation-risk prompt (documentation prompt, physician-review-only)
# --------------------------------------------------------------------- #
class RiskRequest(BaseModel):
    age: float | None = None
    vitals: dict[str, str] | None = None
    complaints: list[dict] | None = None
    hpi: str = ""
    past_history: str = ""
    medications: str = ""


@router.post("/risk")
async def assess_risk(body: RiskRequest, user: CurrentUser = Depends(get_current_user)):
    """Score the encounter recorded so far. No network calls, no cost, no AI
    provider — pure local inference, so it works when everything else is down."""
    payload = body.model_dump()
    payload["completeness_pct"] = completeness.score(payload)["score_pct"]
    result = risk.assess(payload)
    registry.inc("cma_risk_assessments_total",
                 {"escalate": str(result.get("escalate", False)).lower()})
    return result


# --------------------------------------------------------------------- #
# Save — one atomic database transaction
# --------------------------------------------------------------------- #
class NewPatient(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    age: int | None = Field(default=None, ge=0, le=130)
    gender: str | None = None
    phone: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None


class SaveRequest(BaseModel):
    patient_id: str | None = None
    new_patient: NewPatient | None = None
    visit_id: str | None = None
    expected_version: int | None = None       # optimistic lock, from GET /visits/{id}
    status: str = "completed"                 # "completed" | "in_progress"
    transcript: str | None = Field(default=None, max_length=400_000)
    dialogue: list | None = None
    soap: dict | None = None
    entities: dict | None = None
    follow_up_questions: list | None = None
    prescription: list | None = None
    clinical_considerations: dict | None = None
    vitals: dict | None = None
    wizard: dict | None = None
    sign_off: dict | None = None
    consent_given: bool = False
    consent_method: str | None = None
    attested: bool = False


_ENTITY_FACT_TYPES = {"diagnoses": "diagnosis", "allergies": "allergy", "symptoms": "symptom"}


def build_facts(*, entities: dict | None, prescription: list | None, vitals: dict | None) -> list[dict]:
    """Turn the signed encounter into clinical-fact rows.

    Two distinctions the previous version blurred and that matter downstream:

    * A medication the patient *reported* is history; a medication the doctor
      *prescribed* is the current regimen. They are tagged differently so the
      medication timeline does not report stopping a drug that was never started.
    * An allergy statement of "no known drug allergies" is not an allergy. It is
      recorded as a documented-negative rather than as an allergen, so nothing
      downstream tries to match a prescription against it.
    """
    rows: list[dict] = []

    def add(fact_type: str, value: object, structured: dict | None = None,
            clinical_status: str = "current") -> None:
        text = str(value or "").strip()
        if text:
            rows.append({"fact_type": fact_type, "value": text[:500],
                         "structured": structured or {}, "clinical_status": clinical_status,
                         "source": "doctor_confirmed_ai"})

    ent = entities or {}
    for key, fact_type in _ENTITY_FACT_TYPES.items():
        for item in (ent.get(key) or []):
            value = item if not isinstance(item, dict) else (
                item.get("value") or item.get("name") or item.get("text") or ""
            )
            if fact_type == "allergy":
                parsed = completeness.normalise_allergy_statement(str(value))
                if parsed["none_known"]:
                    add("allergy", "No known drug allergies",
                        {"documented_negative": True}, clinical_status="current")
                    continue
                for allergen in parsed["allergens"] or ([str(value)] if value else []):
                    add("allergy", allergen, item if isinstance(item, dict) else {})
                continue
            add(fact_type, value, item if isinstance(item, dict) else {})

    for item in (ent.get("medications") or []):
        value = item if not isinstance(item, dict) else (item.get("value") or item.get("name") or "")
        structured = {"context": "reported"}
        if isinstance(item, dict):
            structured.update(item)
            structured["context"] = "reported"
        add("medication", value, structured, clinical_status="current")

    for rx in (prescription or []):
        if isinstance(rx, dict):
            name = rx.get("generic") or rx.get("drug") or rx.get("name") or rx.get("brand") or ""
            add("medication", name, {**rx, "context": "prescribed"})
        else:
            add("medication", rx, {"context": "prescribed"})

    for metric, reading in (vitals or {}).items():
        if str(reading).strip():
            add("vital", f"{metric}: {reading}",
                {"metric": str(metric), "reading": str(reading)})

    return rows


@router.post("/save")
async def save(body: SaveRequest, user: CurrentUser = Depends(get_current_user)):
    headers = user_headers(user.token)
    is_draft = body.status == "in_progress"

    # Checked here so the caller gets a clear 400 rather than a database error,
    # and again inside finalize_visit() so it cannot be bypassed.
    if not is_draft:
        if not body.attested:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Physician attestation is required before completing a note.")
        if not user.can_attest:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"Role '{user.role}' may not sign a clinical note.")

    patient_id = await _resolve_patient(body, user, headers)

    so = body.soap or {}
    note = {
        "transcript": body.transcript,
        "dialogue": body.dialogue or [],
        "subjective": so.get("subjective"), "objective": so.get("objective"),
        "assessment": so.get("assessment"), "plan": so.get("plan"),
        "entities": body.entities or {},
        "follow_up_questions": body.follow_up_questions or [],
        "prescription": body.prescription or [],
        "clinical_considerations": body.clinical_considerations or {},
        "vitals": body.vitals or {},
        "wizard": body.wizard or {},
        "sign_off": body.sign_off or {},
    }
    facts = [] if is_draft else build_facts(
        entities=body.entities, prescription=body.prescription, vitals=body.vitals,
    )

    resp = await rpc("finalize_visit", headers=headers, args={
        "p_patient_id": patient_id,
        "p_note": note,
        "p_facts": facts,
        "p_visit_id": body.visit_id,
        "p_expected_version": body.expected_version,
        "p_draft": is_draft,
        "p_attested": body.attested,
        "p_consent_given": body.consent_given,
        "p_consent_method": body.consent_method,
    })

    if resp.status_code not in (200, 201):
        raise _translate_db_error(resp)

    result = resp.json()
    registry.inc("cma_visits_saved_total", {"status": result.get("status", "?")})
    return result


async def _resolve_patient(body: SaveRequest, user: CurrentUser, headers: dict) -> str:
    if body.patient_id:
        return body.patient_id
    if not body.new_patient:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide patient_id or new_patient")

    from datetime import date

    from ...core.supabase import rest

    np = body.new_patient
    dob = f"{date.today().year - np.age:04d}-01-01" if np.age else None
    r = await rest("POST", "patients", headers=headers, prefer="return=representation", json={
        "clinic_id": user.clinic_id, "name": np.name, "gender": np.gender,
        "phone": np.phone, "dob": dob, "height_cm": np.height_cm, "weight_kg": np.weight_kg,
    })
    if r.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Patient create failed: {r.text[:300]}")
    return r.json()[0]["id"]


# PostgreSQL SQLSTATEs raised by finalize_visit(), mapped to the HTTP status a
# client can act on. Without this every rule violation surfaces as an opaque
# 502 and the UI cannot tell "someone else edited this" from "the database is
# down".
_SQLSTATE_STATUS = {
    "P0001": status.HTTP_400_BAD_REQUEST,          # raise_exception default
    "23514": status.HTTP_409_CONFLICT,             # check_violation
    "42501": status.HTTP_403_FORBIDDEN,            # insufficient_privilege
    "02000": status.HTTP_404_NOT_FOUND,            # no_data_found
    "40001": status.HTTP_409_CONFLICT,             # serialization_failure
}


def _translate_db_error(resp) -> HTTPException:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    code = str(body.get("code") or "")
    message = str(body.get("message") or body.get("hint") or resp.text[:300])
    http_status = _SQLSTATE_STATUS.get(code, status.HTTP_502_BAD_GATEWAY)
    log.warning("save_rejected", extra={"extra_fields": {"sqlstate": code, "status": http_status}})
    if http_status == status.HTTP_502_BAD_GATEWAY:
        message = "Could not save the visit. Nothing was written; please retry."
    return HTTPException(http_status, message)


# --------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------- #
def _cap(text: str) -> str:
    limit = get_settings().max_transcript_chars
    text = text.strip()
    if len(text) <= limit:
        return text
    # Keep the tail: the end of a consultation carries the plan and the
    # prescription, which are the parts a truncated note must not lose.
    return text[-limit:]


def _pct(value: object) -> int:
    try:
        return max(0, min(100, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _string_list(value: object) -> list[str]:
    """Coerce a model-supplied list into clean strings.

    `str(None)` is `"None"`, which is truthy — so a JSON `null` in a symptom
    array used to reach the physician as the literal word "None" in their note.
    Non-strings are dropped rather than stringified.
    """
    if not isinstance(value, list):
        return []
    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def normalise_considerations(raw: object) -> dict:
    """Coerce the physician-review-only block into a fixed shape.

    Defensive by design: this is model output, and the UI must not have to
    handle a red flag that is a string, a null urgency, or a missing field.
    """
    c = raw if isinstance(raw, dict) else {}
    red_flags = []
    for f in (c.get("red_flags") or []):
        if not isinstance(f, dict) or not f.get("finding"):
            continue
        urgency = str(f.get("urgency", "routine")).lower()
        red_flags.append({
            "finding": str(f.get("finding", "")).strip(),
            "concern": str(f.get("concern", "")).strip(),
            "urgency": urgency if urgency in _URGENCY else "routine",
            "action": str(f.get("action", "")).strip(),
            "source": "ai",
        })
    red_flags.sort(key=lambda x: _URG_RANK.get(x["urgency"], 3))

    investigations = [
        {"test": str(i.get("test", "")).strip(), "rationale": str(i.get("rationale", "")).strip()}
        for i in (c.get("suggested_investigations") or [])
        if isinstance(i, dict) and i.get("test")
    ]
    return {
        "red_flags": red_flags,
        "suggested_investigations": investigations,
        "missing_information": _string_list(c.get("missing_information")),
    }


def _trim_flags(flags: list[dict], keep: int = 4, action_max: int = 200) -> list[dict]:
    """Cap the red-flag panel so it stays scannable. Four is the point at which
    a physician reads the list; beyond it they skim past the whole panel."""
    out = []
    for f in flags[:keep]:
        f["action"] = (f.get("action") or "")[:action_max]
        f["concern"] = (f.get("concern") or "")[:160]
        out.append(f)
    return out


_STOP = {"about", "ask", "for", "the", "and", "any", "check", "assess", "with", "your",
         "patient", "possible", "consider", "rule", "out", "screen", "signs", "symptoms",
         "history", "this", "that", "from", "have", "been", "such", "other"}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 3 and w not in _STOP}


def _dedupe_followups(follow_ups: list[dict], red_flags: list[dict]) -> list[dict]:
    """Drop follow-ups that merely restate a red flag, then sort by severity."""
    flag_keywords = [(_keywords(rf["finding"]) | _keywords(rf["action"])) for rf in red_flags]
    kept = [
        q for q in follow_ups
        if not any(len((_keywords(q.get("question", "")) | _keywords(q.get("concern", ""))) & k) >= 2
                   for k in flag_keywords)
    ]
    kept.sort(key=lambda q: (_SEV_RANK.get(str(q.get("severity", "low")).lower(), 3),
                             -int(q.get("likelihood_pct") or 0)))
    return kept
