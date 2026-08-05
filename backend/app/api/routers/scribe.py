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
import re
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..deps import CurrentUser, get_current_user
from ...core.config import get_settings
from ...core.supabase import audit, rest, user_headers

router = APIRouter(prefix="/scribe")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


# --------------------------------------------------------------------- #
# Speech-to-text — OpenAI gpt-4o-transcribe (preferred), Sarvam fallback
# --------------------------------------------------------------------- #
async def _stt_openai(s, filename: str, audio: bytes, content_type: str) -> dict:
    files = {"file": (filename, audio, content_type)}
    data = {"model": s.openai_stt_model, "response_format": "json"}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(OPENAI_STT_URL, headers={"Authorization": f"Bearer {s.openai_api_key}"},
                              data=data, files=files)
    if r.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"STT error {r.status_code}: {r.text[:300]}")
    return {"transcript": r.json().get("text", ""), "language": None}


async def _stt_sarvam(s, filename: str, audio: bytes, content_type: str) -> dict:
    files = {"file": (filename, audio, content_type)}
    data = {"model": s.sarvam_stt_model, "language_code": s.sarvam_stt_language}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(SARVAM_STT_URL, headers={"api-subscription-key": s.sarvam_api_key},
                              data=data, files=files)
    if r.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"STT error {r.status_code}: {r.text[:300]}")
    body = r.json()
    return {"transcript": body.get("transcript", ""), "language": body.get("language_code")}


@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), user: CurrentUser = Depends(get_current_user)):
    s = get_settings()
    if not s.openai_api_key and not s.sarvam_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "No speech-to-text provider configured (set OPENAI_API_KEY or SARVAM_API_KEY).")
    audio = await file.read()
    fname = file.filename or "audio.wav"
    ctype = file.content_type or "audio/wav"
    try:
        if s.openai_api_key:
            return await _stt_openai(s, fname, audio, ctype)
        return await _stt_sarvam(s, fname, audio, ctype)
    except httpx.HTTPError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"STT request failed: {e}")


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
  or worsening pain, bleeding, high fever with stiff neck, etc.). Be STRICT: only list a red flag
  that fits the body system/region actually involved. For a minor, localised complaint (e.g. a
  simple ankle sprain, common cold, mild rash) return an EMPTY array — do NOT manufacture red
  flags or list conditions unrelated to the presentation. Keep "action" short (<= 15 words).
  "urgency": "emergency" = needs same-day/ED attention; "urgent" = review soon; "routine" = worth noting.
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
    entities = {k: ent.get(k, []) for k in ("symptoms", "medications", "allergies", "diagnoses", "follow_up")}
    fups = [] if body.mode == "final" else [
        {
            "question": q.get("question", ""),
            "concern": q.get("concern", ""),
            "likelihood_pct": max(0, min(100, int(q.get("likelihood_pct") or 0))),
            "severity": q.get("severity", "low"),
        }
        for q in (data.get("follow_up_questions") or []) if isinstance(q, dict) and q.get("question")
    ]

    considerations = _considerations(data.get("clinical_considerations"))
    # Red flags come from the model's contextual reasoning only. The broad KB
    # fuzzy-matcher (kb_ground_red_flags) was retired here — it surfaced conditions
    # unrelated to the presentation (e.g. liver cancer for an ankle sprain). the synthesis service's
    # must-not-miss (decision-support panel) is the grounded safety net now.
    considerations["red_flags"] = _trim_flags(considerations["red_flags"], keep=4, action_max=200)
    # De-duplicate follow-ups against the red flags and sort by severity.
    fups = _dedupe_followups(fups, considerations["red_flags"])

    return {
        "dialogue": [d for d in (data.get("dialogue") or []) if isinstance(d, dict) and d.get("text")],
        "soap": {k: so.get(k, "") for k in ("subjective", "objective", "assessment", "plan")},
        "entities": entities,
        "follow_up_questions": fups,
        "clinical_considerations": considerations,
    }


_URGENCY = {"emergency", "urgent", "routine"}
_URG_RANK = {"emergency": 0, "urgent": 1, "routine": 2}
_SEV_RANK = {"high": 0, "moderate": 1, "low": 2}


def _trim_flags(flags: list[dict], keep: int = 4, action_max: int = 200) -> list[dict]:
    """Cap the number of red flags and truncate their text so the panel stays
    scannable instead of dumping walls of text."""
    out = []
    for f in flags[:keep]:
        f["action"] = (f.get("action") or "")[:action_max]
        f["concern"] = (f.get("concern") or "")[:160]
        out.append(f)
    return out
_STOP = {"about", "ask", "for", "the", "and", "any", "check", "assess", "with", "your",
         "patient", "possible", "consider", "rule", "out", "screen", "signs", "symptoms",
         "history", "this", "that", "from", "have", "been", "such", "other"}


def _rf_order(x: dict) -> tuple:
    # KB-grounded first within same urgency (curated = more trustworthy), then AI.
    return (_URG_RANK.get(x.get("urgency"), 3), 0 if x.get("source") == "kb" else 1)


def _kw(text: str) -> set[str]:
    """Significant word tokens for cheap overlap-based de-duplication."""
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) > 3 and w not in _STOP}


def _grounded_urgency(acuity: str | None, cant_miss: bool) -> str:
    """Loud for genuine can't-miss, quiet otherwise — controls alarm fatigue."""
    a = (acuity or "").lower()
    if cant_miss and any(k in a for k in ("emerg", "critical", "life")):
        return "emergency"
    if cant_miss:
        return "urgent"
    return "routine"  # red-flag-bearing but not flagged can't-miss → gentle note


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
            "source": "ai",
        })
    red_flags.sort(key=_rf_order)
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
# KB grounding: curated red flags from the ICMR-derived knowledge base
# --------------------------------------------------------------------- #
_ABBREV = {
    "sob": "breathlessness", "shortness of breath": "breathlessness",
    "short of breath": "breathlessness", "cp": "chest pain",
    "loc": "loss of consciousness", "abd pain": "abdominal pain",
}


def _norm_symptom(s: str) -> str:
    t = re.sub(r"\b(mild|severe|acute|chronic|slight|a lot of|some|very|feeling|feel)\b", "", s.lower())
    t = t.strip(" .,-")
    return _ABBREV.get(t, t)


async def kb_ground_red_flags(symptoms: list[str], token: str) -> list[dict]:
    """Ask the KB which CAN'T-MISS conditions these symptoms point at, and
    surface each one's curated red-flag features as a grounded red flag."""
    findings = sorted({_norm_symptom(s) for s in symptoms if len(_norm_symptom(s)) >= 3})
    if not findings:
        return []
    try:
        resp = await rest("POST", "rpc/kb_ground_red_flags",
                          headers=user_headers(token), json={"findings": findings})
    except httpx.HTTPError:
        return []
    if resp.status_code != 200:
        return []
    rows = resp.json() or []

    by_cond: dict[str, dict] = {}
    for r in rows:
        cid = r.get("condition_id")
        if not cid:
            continue
        c = by_cond.setdefault(cid, {
            "name": r.get("condition_name", ""),
            "acuity": r.get("acuity"),
            "prevalence": r.get("prevalence_tier"),
            "cant_miss": bool(r.get("any_cantmiss")),
            "matched": r.get("matched_count") or 0,
            "score": float(r.get("score") or 0),
            "features": [], "actions": [],
        })
        lbl = (r.get("redflag_label") or "").strip()
        if lbl and lbl.lower() not in {x.lower() for x in c["features"]}:
            c["features"].append(lbl)
        act = (r.get("action") or "").strip()
        if act and act.lower() not in {x.lower() for x in c["actions"]}:
            c["actions"].append(act)

    grounded: list[dict] = []
    # can't-miss first, then by term-specificity score (v3), then match breadth
    ranked = sorted(by_cond.items(),
                    key=lambda kv: (not kv[1]["cant_miss"], -kv[1]["score"], -kv[1]["matched"]))
    for cid, c in ranked[:4]:
        feats = ", ".join(c["features"][:5])
        action = "; ".join(c["actions"][:2]) if c["actions"] else ""
        if feats:
            action = (f"Screen for: {feats}." + (f" {action}" if action else "")).strip()
        concern = ("Can't-miss condition matched to the presenting symptoms (ICMR KB)."
                   if c["cant_miss"] else
                   "Condition with red-flag features matched to the symptoms (ICMR KB).")
        grounded.append({
            "finding": f"Rule out {c['name']}",
            "concern": concern,
            "urgency": _grounded_urgency(c["acuity"], c["cant_miss"]),
            "action": action or f"Consider features of {c['name']}.",
            "source": "kb",
        })
    return grounded


_NAME_GENERIC = {"acute", "chronic", "syndrome", "disease", "disorder", "infection",
                 "primary", "secondary", "possible", "suspected"}


def _merge_considerations(cc: dict, grounded: list[dict]) -> dict:
    """Put curated KB red flags first, then AI red flags that add something new."""
    if not grounded:
        return cc
    # Specific condition-name tokens from the KB flags (e.g. "meningitis",
    # "coronary") — a single shared one signals the AI flag is the same concern.
    kb_name_kw = set()
    for g in grounded:
        name = re.sub(r"^\s*rule out\s+", "", g["finding"], flags=re.IGNORECASE)
        kb_name_kw |= (_kw(name) - _NAME_GENERIC)
    ai_kept = []
    for rf in cc.get("red_flags", []):
        ai_kw = _kw(rf["finding"]) | _kw(rf["concern"])
        if ai_kw & kb_name_kw:      # same underlying condition already covered by KB
            continue
        ai_kept.append(rf)
    merged = grounded + ai_kept
    merged.sort(key=_rf_order)
    cc["red_flags"] = merged
    return cc


def _dedupe_followups(fups: list[dict], red_flags: list[dict]) -> list[dict]:
    """Remove follow-ups that just restate a red flag, then sort by severity."""
    rf_kw = [(_kw(rf["finding"]) | _kw(rf["action"])) for rf in red_flags]
    kept = []
    for q in fups:
        qk = _kw(q.get("question", "")) | _kw(q.get("concern", ""))
        if any(len(qk & k) >= 2 for k in rf_kw):
            continue
        kept.append(q)
    kept.sort(key=lambda q: (_SEV_RANK.get(str(q.get("severity", "low")).lower(), 3),
                             -int(q.get("likelihood_pct") or 0)))
    return kept


# --------------------------------------------------------------------- #
# Live consultation — fast, low-latency pass over the running transcript
# --------------------------------------------------------------------- #
class LiveRequest(BaseModel):
    transcript: str
    patient_context: str | None = None


def _live_prompt(context: str, transcript: str) -> str:
    return (
        "You are a live clinical documentation assistant listening to an ongoing doctor-patient "
        "consultation that may mix Hindi and English (Hinglish). You are NOT a doctor and never "
        "diagnose or prescribe. Work fast. From the running transcript so far, return STRICT JSON:\n"
        '{\n'
        '  "translation": "a clean ENGLISH translation/paraphrase of the conversation so far (concise)",\n'
        '  "symptoms": ["key presenting symptoms mentioned so far"],\n'
        '  "red_flags": [{"finding":"the concerning symptom/sign","concern":"serious condition it could indicate",'
        '"urgency":"emergency|urgent|routine","action":"one short suggested action (<= 12 words)"}],\n'
        '  "questions": [{"question":"a brief instruction to the doctor starting with Ask/Check/Assess",'
        '"severity":"low|moderate|high"}]\n'
        "}\n"
        "RED FLAGS — be strict: include ONLY genuine danger signs that actually FIT this "
        "presentation and are supported by the transcript (e.g. chest pain, breathlessness at rest, "
        "neuro deficits, severe/worsening pain, bleeding). If the complaint is minor/localised "
        "(e.g. a simple ankle sprain, common cold), return an EMPTY red_flags array. Never list a "
        "condition that doesn't match the body system involved. Keep every field short. "
        "Give 2-4 questions. Everything is physician-review-only. Do not invent facts.\n\n"
        f"PATIENT CONTEXT (may be empty):\n{context or '(none)'}\n\n"
        f"RUNNING TRANSCRIPT:\n{transcript}"
    )


async def _gemini_json(prompt: str, max_tokens: int = 2048) -> dict | None:
    """Compact Gemini JSON call with model fallback — used by the live lane."""
    s = get_settings()
    if not s.gemini_api_key:
        return None
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2, "maxOutputTokens": max_tokens},
    }
    models: list[str] = []
    for m in [s.gemini_model, "gemini-2.0-flash", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"]:
        if m and m not in models:
            models.append(m)
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt, model in enumerate(models):
            url = f"{GEMINI_BASE}/models/{model}:generateContent"
            try:
                r = await client.post(url, params={"key": s.gemini_api_key}, json=payload)
            except httpx.HTTPError:
                r = None
            else:
                if r.status_code == 200:
                    return _parse_gemini_json(r.json())
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
            if attempt < len(models) - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
    return None


@router.post("/live")
async def live(body: LiveRequest, user: CurrentUser = Depends(get_current_user)):
    if len(body.transcript.strip()) < 3:
        return {"translation": "", "symptoms": [], "red_flags": [], "questions": []}
    data = await _gemini_json(_live_prompt(body.patient_context or "", body.transcript), 2048) or {}
    symptoms = [str(x).strip() for x in (data.get("symptoms") or []) if str(x).strip()][:10]
    # Contextual red flags from the model ONLY — the broad KB fuzzy-matcher
    # surfaced irrelevant conditions (e.g. liver cancer for an ankle sprain), so
    # it's intentionally not used in the live lane. Keep it tight: top 3, short.
    cc = _considerations({"red_flags": data.get("red_flags"), "completeness_pct": 0})
    red_flags = []
    for rf in cc["red_flags"][:3]:
        rf["action"] = (rf.get("action") or "")[:140]
        rf["concern"] = (rf.get("concern") or "")[:120]
        red_flags.append(rf)
    questions = [
        {"question": str(q.get("question", "")).strip()[:160],
         "severity": str(q.get("severity", "low")).strip().lower()}
        for q in (data.get("questions") or []) if isinstance(q, dict) and q.get("question")
    ][:4]
    return {
        "translation": str(data.get("translation", "")).strip(),
        "symptoms": symptoms,
        "red_flags": red_flags,
        "questions": questions,
    }


# --------------------------------------------------------------------- #
# Structured encounter extraction — fills the consultation wizard fields
# --------------------------------------------------------------------- #
class ExtractRequest(BaseModel):
    transcript: str
    patient_context: str | None = None


def _extract_prompt(context: str, transcript: str) -> str:
    return (
        "You are a clinical documentation assistant. From the doctor-patient consultation "
        "transcript (which may mix Hindi and English), extract a STRUCTURED encounter in clinical "
        "English. You are NOT a doctor; do not diagnose or prescribe. Return STRICT JSON:\n"
        '{\n'
        '  "chief_complaints": [{"text":"symptom in a few words","duration":"e.g. 2 days or empty"}],\n'
        '  "hpi": "history of present illness, concise clinical prose",\n'
        '  "past_history": "", "allergies": "", "medications": "current medications",\n'
        '  "general_exam": "general examination findings if mentioned",\n'
        '  "systemic_exam": "systemic examination findings if mentioned",\n'
        '  "vitals": {"bp":"e.g. 120/80","hr":"","temp":"","spo2":"","rr":"","weight":"","height":""}\n'
        "}\n"
        "Only fill fields actually supported by the transcript; use empty string / empty array "
        "otherwise. Do NOT invent findings or vitals. Keep it faithful to what was said.\n\n"
        f"PATIENT CONTEXT (may be empty):\n{context or '(none)'}\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )


@router.post("/extract")
async def extract(body: ExtractRequest, user: CurrentUser = Depends(get_current_user)):
    empty = {"chief_complaints": [], "hpi": "", "past_history": "", "allergies": "",
             "medications": "", "general_exam": "", "systemic_exam": "", "vitals": {}}
    if len(body.transcript.strip()) < 3:
        return empty
    data = await _gemini_json(_extract_prompt(body.patient_context or "", body.transcript), 4096)
    if not data:
        return empty
    cc = []
    for c in (data.get("chief_complaints") or []):
        if isinstance(c, dict) and c.get("text"):
            cc.append({"text": str(c["text"]).strip()[:120], "duration": str(c.get("duration", "")).strip()[:40]})
        elif isinstance(c, str) and c.strip():
            cc.append({"text": c.strip()[:120], "duration": ""})
    v = data.get("vitals") if isinstance(data.get("vitals"), dict) else {}
    vitals = {k: str(v.get(k, "")).strip() for k in ("bp", "hr", "temp", "spo2", "rr", "weight", "height") if str(v.get(k, "")).strip()}
    return {
        "chief_complaints": cc[:10],
        "hpi": str(data.get("hpi", "")).strip(),
        "past_history": str(data.get("past_history", "")).strip(),
        "allergies": str(data.get("allergies", "")).strip(),
        "medications": str(data.get("medications", "")).strip(),
        "general_exam": str(data.get("general_exam", "")).strip(),
        "systemic_exam": str(data.get("systemic_exam", "")).strip(),
        "vitals": vitals,
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
    visit_id: str | None = None               # set when finalising/updating a draft
    status: str = "completed"                 # "completed" | "in_progress" (draft)
    transcript: str | None = None
    dialogue: list | None = None
    soap: dict | None = None
    entities: dict | None = None
    follow_up_questions: list | None = None
    prescription: list | None = None
    clinical_considerations: dict | None = None
    vitals: dict | None = None
    wizard: dict | None = None                # structured wizard state, for resuming drafts
    # Consent + physician attestation (P0 safety layer).
    consent_given: bool = False
    consent_method: str | None = None       # 'verbal' | 'written'
    attested: bool = False                    # physician's "I reviewed & approve"


@router.post("/save")
async def save(body: SaveRequest, user: CurrentUser = Depends(get_current_user)):
    h = user_headers(user.token)
    is_draft = body.status == "in_progress"

    # A completed note is a permanent clinical record — attestation is required.
    # A draft (in_progress) is a work-in-progress and does not require attestation.
    if not is_draft and not body.attested:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Physician attestation is required before completing a note.")

    now = datetime.now(timezone.utc).isoformat()
    visit_status = "in_progress" if is_draft else "approved"

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

    # The note payload (shared by create + update).
    so = body.soap or {}
    note = {
        "transcript": body.transcript, "dialogue": body.dialogue or [],
        "subjective": so.get("subjective"), "objective": so.get("objective"),
        "assessment": so.get("assessment"), "plan": so.get("plan"),
        "entities": body.entities or {}, "follow_up_questions": body.follow_up_questions or [],
        "prescription": body.prescription or [],
        "clinical_considerations": body.clinical_considerations or {},
        "vitals": body.vitals or {},
        "wizard": body.wizard or {},
    }
    attest = ({"attested": True, "attested_at": now, "attested_by": user.user_id}
              if not is_draft else {"attested": False})
    consent = ({"consent_given": True, "consent_at": now, "consent_method": body.consent_method}
               if body.consent_given else {})

    # 2. Update an existing draft, or create a new visit + note.
    if body.visit_id:
        vp = {"status": visit_status, "approved_at": (None if is_draft else now), **consent}
        v = await rest("PATCH", "visits", headers=h, prefer="return=representation",
                       params={"id": f"eq.{body.visit_id}"}, json=vp)
        if v.status_code not in (200, 204) or (v.status_code == 200 and not v.json()):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Visit update failed: {v.text[:300]}")
        visit_id = body.visit_id
        n = await rest("PATCH", "soap_notes", headers=h, params={"visit_id": f"eq.{visit_id}"},
                       json={**note, **attest})
        if n.status_code not in (200, 204):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Note update failed: {n.text[:300]}")
        action = "complete_visit" if not is_draft else "update_draft"
    else:
        v = await rest("POST", "visits", headers=h, prefer="return=representation", json={
            "patient_id": patient_id, "clinic_id": user.clinic_id, "doctor_id": user.user_id,
            "status": visit_status, "approved_at": (None if is_draft else now), **consent,
        })
        if v.status_code not in (200, 201):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Visit create failed: {v.text[:300]}")
        visit_id = v.json()[0]["id"]
        n = await rest("POST", "soap_notes", headers=h, prefer="return=representation", json={
            "visit_id": visit_id, "patient_id": patient_id, "clinic_id": user.clinic_id,
            "created_by": user.user_id, **note, **attest,
        })
        if n.status_code not in (200, 201):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Note save failed: {n.text[:300]}")
        action = "save_draft" if is_draft else "save_visit"

    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action=action,
                entity="visit", entity_id=visit_id,
                after={"patient_id": patient_id, "status": visit_status})
    return {"visit_id": visit_id, "patient_id": patient_id, "status": visit_status}
