"""AI Clinical Scribe — a background conversation listener.

  POST /scribe/transcribe  — audio (multipart) -> transcript          (Sarvam STT)
  POST /scribe/soap        — transcript -> doctor/patient dialogue,
                             SOAP note, entities, follow-up questions  (Gemini)
  POST /scribe/save        — save the reviewed visit to a patient
                             (existing or new) with today's date.

Everything AI produces is a draft the physician reviews before saving.
"""
import asyncio
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
    "or rule out serious conditions; (5) produce PHYSICIAN-REVIEW-ONLY clinical considerations: "
    "red-flag findings (symptoms/signs that could indicate a serious or emergent condition and "
    "warrant urgent attention), missing information the history is lacking, and investigations "
    "the doctor could consider. These considerations are decision-support prompts for the "
    "physician — they are NEVER a diagnosis or an order. Base everything on the transcript AND "
    "the provided patient history/context — you may reference relevant known history (conditions, "
    "current medications, allergies, recurring issues) to make the note continuity-aware — but "
    "never invent facts beyond what the transcript and context provide. Everything is for "
    "physician review only. Return STRICT JSON."
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
  ],
  "clinical_considerations": {{
    "red_flags": [
      {{"finding": "the concerning symptom/sign from THIS consultation or history",
        "concern": "the serious condition it could indicate",
        "urgency": "emergency" | "urgent" | "routine",
        "action": "brief suggested action for the physician to consider"}}
    ],
    "missing_information": [""],
    "suggested_investigations": [{{"test": "", "rationale": ""}}],
    "completeness_pct": 0
  }}
}}

Formatting rules:
- "assessment": write as SHORT bullet points for fast reading — each line begins with "- ",
  terse phrases (a brief differential + key uncertainties). NOT a paragraph.
- "subjective", "objective", "plan": concise prose.
- Each follow-up "question": phrase it as a brief INSTRUCTION to the doctor, starting with
  "Ask about", "Ask for", "Check for", or "Assess" (e.g. "Ask about shortness of breath at rest").
  Do NOT write it as a verbatim question to the patient.
- clinical_considerations.red_flags: ONLY include genuine red flags actually supported by the
  transcript/history (e.g. chest pain/pressure, breathlessness at rest, neuro deficits, severe
  or worsening pain, bleeding, high fever with stiff neck, etc.). If there are none, return an
  empty array — do NOT manufacture red flags. "urgency": "emergency" = needs same-day/ED
  attention; "urgent" = review soon; "routine" = worth noting.
- "missing_information": key history/exam items not yet covered that a thorough physician would
  want (brief phrases). "suggested_investigations": tests the physician could CONSIDER, each with
  a one-line rationale — never phrased as an order.
- "completeness_pct" (0-100): how complete this consultation record looks (history depth, red-flag
  screening, exam, plan).
Provide 3-5 follow-ups. likelihood_pct (0-100) = how clinically important it is, given the
presentation. Do not invent findings. Empty strings/arrays where unknown. Everything in
clinical_considerations is PHYSICIAN-REVIEW-ONLY assistance, not a diagnosis or prescription."""


def _salvage_json(text: str) -> dict | None:
    """Best-effort recovery of a JSON object from possibly-truncated / fenced text."""
    t = text.strip()
    if t.startswith("```"):  # strip ```json ... ``` fences
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.lstrip("`")
        if t.lower().startswith("json"):
            t = t[4:]
        t = t.strip().rstrip("`").strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Truncated mid-object: keep from first "{" and repair the tail.
    start = t.find("{")
    if start == -1:
        return None
    frag = t[start:]
    # If we're inside an unterminated string, close it (count unescaped quotes).
    quotes = sum(1 for i, c in enumerate(frag) if c == '"' and (i == 0 or frag[i - 1] != "\\"))
    if quotes % 2 == 1:
        frag += '"'
    # Drop a dangling trailing comma before closing.
    frag = frag.rstrip()
    if frag.endswith(","):
        frag = frag[:-1]
    depth_arr = frag.count("[") - frag.count("]")
    if depth_arr > 0:
        frag += "]" * depth_arr
    depth_obj = frag.count("{") - frag.count("}")
    if depth_obj > 0:
        frag += "}" * depth_obj
    try:
        return json.loads(frag)
    except json.JSONDecodeError:
        return None


def _parse_gemini_json(body: dict) -> dict | None:
    """Pull the JSON object out of a Gemini response, tolerating fences,
    multi-part text, and truncation."""
    try:
        cand = (body.get("candidates") or [])[0]
        parts = ((cand.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    except (IndexError, AttributeError, TypeError):
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _salvage_json(text)


@router.post("/soap")
async def soap(body: SoapRequest, user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not s.gemini_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GEMINI_API_KEY not configured")
    if len(body.transcript.strip()) < 3:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Transcript is empty")

    payload = {
        "contents": [{"parts": [{"text": _prompt(body.transcript, body.patient_context or "", body.mode)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            # Final notes are longer; without a generous cap the JSON can be
            # truncated mid-object, which then fails to parse.
            "maxOutputTokens": 8192,
        },
    }

    # Gemini free tier intermittently returns 503 (model overloaded) or 429 (rate
    # limit). These are transient. Strategy: retry with exponential backoff AND
    # rotate through fallback models so a single overloaded model can't block the
    # note. The configured model is tried first; extras are de-duped.
    _FALLBACKS = ["gemini-2.0-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]
    models: list[str] = []
    for m in [s.gemini_model, *_FALLBACKS]:
        if m and m not in models:
            models.append(m)

    r = None
    last_err = ""
    overloaded = False
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt, model in enumerate(models):
            url = f"{GEMINI_BASE}/models/{model}:generateContent"
            try:
                r = await client.post(url, params={"key": s.gemini_api_key}, json=payload)
            except httpx.HTTPError as e:
                last_err = f"SOAP request failed: {e}"
                r = None
            else:
                if r.status_code == 200:
                    break
                last_err = f"SOAP error {r.status_code} ({model}): {r.text[:300]}"
                overloaded = r.status_code in (429, 503)
                if r.status_code not in (429, 500, 502, 503, 504):
                    break  # non-transient (bad key, bad request) — stop early
            if attempt < len(models) - 1:
                await asyncio.sleep(1.5 * (attempt + 1))  # brief pause before next model

    if r is None or r.status_code != 200:
        detail = last_err or "SOAP request failed"
        if overloaded:
            detail = "The AI models are briefly overloaded. Please press Analyse again in a few seconds."
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail)
    data = _parse_gemini_json(r.json())
    if data is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "The AI could not produce a clean note this time. Please press "
                            "Analyse/Finalise again.")

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
        "clinical_considerations": _considerations(data.get("clinical_considerations")),
    }


_URGENCY = {"emergency", "urgent", "routine"}


def _considerations(c: object) -> dict:
    """Normalise the physician-review-only considerations block."""
    c = c if isinstance(c, dict) else {}
    red_flags = []
    for f in (c.get("red_flags") or []):
        if not isinstance(f, dict) or not f.get("finding"):
            continue
        u = str(f.get("urgency", "routine")).lower()
        red_flags.append({
            "finding": str(f.get("finding", "")).strip(),
            "concern": str(f.get("concern", "")).strip(),
            "urgency": u if u in _URGENCY else "routine",
            "action": str(f.get("action", "")).strip(),
        })
    # emergency first, then urgent, then routine
    order = {"emergency": 0, "urgent": 1, "routine": 2}
    red_flags.sort(key=lambda x: order.get(x["urgency"], 3))
    investigations = [
        {"test": str(i.get("test", "")).strip(), "rationale": str(i.get("rationale", "")).strip()}
        for i in (c.get("suggested_investigations") or [])
        if isinstance(i, dict) and i.get("test")
    ]
    missing = [str(m).strip() for m in (c.get("missing_information") or []) if str(m).strip()]
    try:
        pct = max(0, min(100, int(c.get("completeness_pct") or 0)))
    except (TypeError, ValueError):
        pct = 0
    return {
        "red_flags": red_flags,
        "missing_information": missing,
        "suggested_investigations": investigations,
        "completeness_pct": pct,
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
    prescription: list | None = None
    clinical_considerations: dict | None = None
    # Consent + physician attestation (P0 safety layer).
    consent_given: bool = False
    consent_method: str | None = None       # 'verbal' | 'written'
    attested: bool = False                    # physician's "I reviewed & approve"


@router.post("/save")
async def save(body: SaveRequest, user: CurrentUser = Depends(get_current_user)):
    h = user_headers(user.token)

    # 0. AI-generated content is never permanent without the physician's explicit
    #    approval. Enforce attestation server-side (not just in the UI).
    if not body.attested:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Physician attestation is required before saving a note.")

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

    # 2. Create the visit (approved) with recording consent.
    now = datetime.now(timezone.utc).isoformat()
    v = await rest("POST", "visits", headers=h, prefer="return=representation", json={
        "patient_id": patient_id, "clinic_id": user.clinic_id, "doctor_id": user.user_id,
        "status": "approved", "approved_at": now,
        "consent_given": bool(body.consent_given),
        "consent_at": now if body.consent_given else None,
        "consent_method": body.consent_method if body.consent_given else None,
    })
    if v.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Visit create failed: {v.text[:300]}")
    visit_id = v.json()[0]["id"]

    # 3. Store the note, with considerations + the physician's attestation.
    so = body.soap or {}
    n = await rest("POST", "soap_notes", headers=h, prefer="return=representation", json={
        "visit_id": visit_id, "patient_id": patient_id, "clinic_id": user.clinic_id,
        "transcript": body.transcript, "dialogue": body.dialogue or [],
        "subjective": so.get("subjective"), "objective": so.get("objective"),
        "assessment": so.get("assessment"), "plan": so.get("plan"),
        "entities": body.entities or {}, "follow_up_questions": body.follow_up_questions or [],
        "prescription": body.prescription or [],
        "clinical_considerations": body.clinical_considerations or {},
        "attested": True, "attested_at": now, "attested_by": user.user_id,
        "created_by": user.user_id,
    })
    if n.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Note save failed: {n.text[:300]}")

    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="save_visit",
                entity="visit", entity_id=visit_id,
                after={"patient_id": patient_id, "attested_by": user.user_id,
                       "consent_given": bool(body.consent_given)})
    return {"visit_id": visit_id, "patient_id": patient_id}
