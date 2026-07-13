"""AI Clinical Scribe — a background conversation listener.

  POST /scribe/transcribe  — audio (multipart) -> transcript          (Sarvam STT)
  POST /scribe/soap        — transcript -> doctor/patient dialogue,
                             SOAP note, entities, follow-up questions  (Gemini)
  POST /scribe/save        — save the reviewed visit to a patient
                             (existing or new) with today's date.

Everything AI produces is a draft the physician reviews before saving.
"""
import json
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..deps import CurrentUser, get_current_user
from ...core.config import get_settings
from ...core.supabase import audit, rest, user_headers

router = APIRouter(prefix="/scribe")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------- #
# Speech-to-text (Sarvam)
# --------------------------------------------------------------------- #
@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not s.sarvam_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SARVAM_API_KEY not configured")
    audio = await file.read()
    files = {"file": (file.filename or "audio.wav", audio, file.content_type or "audio/wav")}
    data = {"model": s.sarvam_stt_model, "language_code": s.sarvam_stt_language}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(SARVAM_STT_URL, headers={"api-subscription-key": s.sarvam_api_key},
                                  data=data, files=files)
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"STT request failed: {e}")
    if r.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"STT error {r.status_code}: {r.text[:300]}")
    body = r.json()
    return {"transcript": body.get("transcript", ""), "language": body.get("language_code")}


# --------------------------------------------------------------------- #
# Analysis: dialogue + SOAP + entities + follow-up questions (Gemini)
# --------------------------------------------------------------------- #
class SoapRequest(BaseModel):
    transcript: str
    patient_context: str | None = None
    mode: str = "interim"  # "interim" (SOAP + follow-ups) | "final" (complete note, no questions)


_SYSTEM = (
    "You are an AI clinical documentation assistant listening to a doctor-patient "
    "consultation (which may mix Hindi and English / Hinglish). You are NOT a doctor and "
    "never diagnose or prescribe. From the transcript you must: (1) reconstruct the "
    "conversation as a back-and-forth, labelling each turn as the doctor or the patient "
    "(the doctor asks questions and gives advice; the patient describes symptoms and answers); "
    "(2) write a concise SOAP note in clinical English based on that exchange; "
    "(3) extract key entities; (4) suggest follow-up questions the doctor could ask to clarify "
    "or rule out serious conditions. Base the note on the transcript AND the provided patient "
    "history/context — you may reference relevant known history (conditions, current medications, "
    "allergies, recurring issues) to make the note continuity-aware — but never invent facts "
    "beyond what the transcript and context provide. Everything is for physician review only. "
    "Return STRICT JSON."
)


def _prompt(transcript: str, context: str, mode: str) -> str:
    if mode == "final":
        mode_note = (
            "This is the FINAL, COMPLETE note for the ENTIRE consultation (the transcript spans "
            "the whole visit, possibly across several recorded segments). Produce a thorough "
            "record: full subjective history, all objective findings mentioned, a bulleted "
            "assessment, and a COMPLETE plan INCLUDING any medications/prescriptions and advice "
            "discussed. Return an EMPTY follow_up_questions array — no more questions."
        )
    else:
        mode_note = (
            "This is an INTERIM note during an ONGOING consultation. Summarise everything so far "
            "and provide 3-5 follow-up suggestions the doctor could still explore."
        )
    return f"""{_SYSTEM}

{mode_note}

PATIENT CONTEXT (may be empty):
{context or "(none)"}

CONSULTATION TRANSCRIPT (may combine several recorded segments):
{transcript}

Return JSON with exactly this shape:
{{
  "dialogue": [{{"speaker": "doctor" | "patient", "text": ""}}],
  "soap": {{"subjective": "", "objective": "", "assessment": "", "plan": ""}},
  "entities": {{"symptoms": [], "medications": [], "allergies": [], "diagnoses": [], "follow_up": []}},
  "follow_up_questions": [
    {{"question": "", "concern": "what this probes / could reveal",
      "likelihood_pct": 0, "severity": "low" | "moderate" | "high"}}
  ]
}}

Formatting rules:
- "assessment": write as SHORT bullet points for fast reading — each line begins with "- ",
  terse phrases (a brief differential + key uncertainties). NOT a paragraph.
- "subjective", "objective", "plan": concise prose.
- Each follow-up "question": phrase it as a brief INSTRUCTION to the doctor, starting with
  "Ask about", "Ask for", "Check for", or "Assess" (e.g. "Ask about shortness of breath at rest").
  Do NOT write it as a verbatim question to the patient.
Provide 3-5 follow-ups. likelihood_pct (0-100) = how clinically important it is, given the
presentation. Do not invent findings. Empty strings/arrays where unknown."""


@router.post("/soap")
async def soap(body: SoapRequest, user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not s.gemini_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GEMINI_API_KEY not configured")
    if len(body.transcript.strip()) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transcript is empty")

    url = f"{GEMINI_BASE}/models/{s.gemini_model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": _prompt(body.transcript, body.patient_context or "", body.mode)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, params={"key": s.gemini_api_key}, json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"SOAP request failed: {e}")
    if r.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"SOAP error {r.status_code}: {r.text[:400]}")
    try:
        data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Model returned unexpected output")

    so = data.get("soap") or {}
    ent = data.get("entities") or {}
    fups = [] if body.mode == "final" else [
        {
            "question": q.get("question", ""),
            "concern": q.get("concern", ""),
            "likelihood_pct": max(0, min(100, int(q.get("likelihood_pct") or 0))),
            "severity": q.get("severity", "low"),
        }
        for q in (data.get("follow_up_questions") or []) if isinstance(q, dict) and q.get("question")
    ]
    return {
        "dialogue": [d for d in (data.get("dialogue") or []) if isinstance(d, dict) and d.get("text")],
        "soap": {k: so.get(k, "") for k in ("subjective", "objective", "assessment", "plan")},
        "entities": {k: ent.get(k, []) for k in ("symptoms", "medications", "allergies", "diagnoses", "follow_up")},
        "follow_up_questions": fups,
    }


# --------------------------------------------------------------------- #
# Save the reviewed visit to a patient (existing or new)
# --------------------------------------------------------------------- #
class NewPatient(BaseModel):
    name: str
    age: int | None = None
    gender: str | None = None
    phone: str | None = None
    height_cm: float | None = None
    weight_kg: float | None = None


class SaveRequest(BaseModel):
    patient_id: str | None = None
    new_patient: NewPatient | None = None
    transcript: str | None = None
    dialogue: list | None = None
    soap: dict | None = None
    entities: dict | None = None
    follow_up_questions: list | None = None


@router.post("/save")
async def save(body: SaveRequest, user: CurrentUser = Depends(get_current_user)):
    h = user_headers(user.token)

    # 1. Resolve or create the patient.
    if body.patient_id:
        patient_id = body.patient_id
    elif body.new_patient:
        np = body.new_patient
        dob = f"{date.today().year - np.age:04d}-01-01" if np.age else None
        payload = {
            "clinic_id": user.clinic_id, "name": np.name, "gender": np.gender,
            "phone": np.phone, "dob": dob, "height_cm": np.height_cm, "weight_kg": np.weight_kg,
        }
        r = await rest("POST", "patients", headers=h, json=payload, prefer="return=representation")
        if r.status_code not in (200, 201):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Patient create failed: {r.text[:300]}")
        patient_id = r.json()[0]["id"]
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Provide patient_id or new_patient")

    # 2. Create the visit (approved).
    now = datetime.now(timezone.utc).isoformat()
    v = await rest("POST", "visits", headers=h, prefer="return=representation", json={
        "patient_id": patient_id, "clinic_id": user.clinic_id, "doctor_id": user.user_id,
        "status": "approved", "approved_at": now,
    })
    if v.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Visit create failed: {v.text[:300]}")
    visit_id = v.json()[0]["id"]

    # 3. Store the note.
    so = body.soap or {}
    n = await rest("POST", "soap_notes", headers=h, prefer="return=representation", json={
        "visit_id": visit_id, "patient_id": patient_id, "clinic_id": user.clinic_id,
        "transcript": body.transcript, "dialogue": body.dialogue or [],
        "subjective": so.get("subjective"), "objective": so.get("objective"),
        "assessment": so.get("assessment"), "plan": so.get("plan"),
        "entities": body.entities or {}, "follow_up_questions": body.follow_up_questions or [],
        "created_by": user.user_id,
    })
    if n.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Note save failed: {n.text[:300]}")

    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="save_visit",
                entity="visit", entity_id=visit_id, after={"patient_id": patient_id})
    return {"visit_id": visit_id, "patient_id": patient_id}
