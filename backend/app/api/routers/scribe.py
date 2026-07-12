"""AI Clinical Scribe.

  POST /scribe/transcribe  — audio (multipart) -> transcript   (Sarvam STT)
  POST /scribe/soap        — transcript -> SOAP note + entities (Gemini)

Both require an authenticated clinic user. LLM/STT are the only AI in the app.
The physician reviews and edits every output before it is saved.

Gemini is called via its REST API with httpx (no SDK) for a clean, consistent
HTTP layer across the app.
"""
import json

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..deps import CurrentUser, get_current_user
from ...core.config import get_settings

router = APIRouter(prefix="/scribe")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------- #
# Speech-to-text (Sarvam)
# --------------------------------------------------------------------- #
@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
):
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
# SOAP generation (Gemini REST)
# --------------------------------------------------------------------- #
class SoapRequest(BaseModel):
    transcript: str
    patient_context: str | None = None


_SYSTEM = (
    "You are an AI clinical documentation assistant. You are NOT a doctor and never "
    "diagnose or prescribe. Given a doctor-patient consultation transcript (which may mix "
    "Hindi and English / Hinglish), produce a concise, structured SOAP note in clinical "
    "English and extract key entities. Only include information supported by the transcript. "
    "The physician will review and edit everything. Return STRICT JSON only."
)


def _prompt(transcript: str, context: str) -> str:
    return f"""{_SYSTEM}

PATIENT CONTEXT (may be empty):
{context or "(none)"}

CONSULTATION TRANSCRIPT:
{transcript}

Return JSON with exactly this shape:
{{
  "soap": {{"subjective": "", "objective": "", "assessment": "", "plan": ""}},
  "entities": {{
    "symptoms": [], "medications": [], "allergies": [], "diagnoses": [], "follow_up": []
  }}
}}
Use empty strings/arrays where the transcript gives no information. Do not invent findings."""


@router.post("/soap")
async def soap(body: SoapRequest, user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not s.gemini_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GEMINI_API_KEY not configured")
    if len(body.transcript.strip()) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transcript is empty")

    url = f"{GEMINI_BASE}/models/{s.gemini_model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": _prompt(body.transcript, body.patient_context or "")}]}],
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
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError, TypeError):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Model returned unexpected output")

    soap_obj = data.get("soap") or {}
    ent = data.get("entities") or {}
    return {
        "soap": {
            "subjective": soap_obj.get("subjective", ""),
            "objective": soap_obj.get("objective", ""),
            "assessment": soap_obj.get("assessment", ""),
            "plan": soap_obj.get("plan", ""),
        },
        "entities": {
            "symptoms": ent.get("symptoms", []),
            "medications": ent.get("medications", []),
            "allergies": ent.get("allergies", []),
            "diagnoses": ent.get("diagnoses", []),
            "follow_up": ent.get("follow_up", []),
        },
    }
