"""Clinical Synthesis API — decision-support proxy.

We call the synthesis service's production clinical brain (ICMR/MoHFW-grounded DDx, investigations,
treatment) from OUR backend only — the base URL is a shared secret and the upstream
is unauthenticated, so it must never be reachable from the browser. Everything here
is PHYSICIAN-REVIEW-ONLY decision support; the physician remains the decision maker.

Endpoints:
  POST /synthesis/decision-support  — symptoms -> DDx + must-not-miss + investigations
                                       + empiric treatment (3 lanes fired in parallel).
  POST /synthesis/confirm           — a chosen diagnosis -> locked investigations + treatment.

Fail-open: on any upstream error we return empty lists (never a 5xx mid-encounter),
mirroring the synthesis service's own contract.
"""
import asyncio

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import CurrentUser, get_current_user
from ...core.config import get_settings

router = APIRouter(prefix="/synthesis")


class DecisionSupportRequest(BaseModel):
    chief_complaints: list[str]
    age: str | None = None
    gender: str | None = None
    patient_weight: str | None = None
    duration: str | None = None
    vitals: dict | None = None


class ConfirmRequest(BaseModel):
    chief_complaints: list[str]
    confirmed_diagnoses: list[str]
    age: str | None = None
    gender: str | None = None
    patient_weight: str | None = None
    vitals: dict | None = None


def _base() -> str:
    return (get_settings().synthesis_api_base or "").rstrip("/")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = get_settings().synthesis_api_key
    if key:
        h["X-API-Key"] = key
    return h


def _payload(body: BaseModel) -> dict:
    # Only forward fields Clinical Synthesis expects, dropping empties.
    d = body.model_dump(exclude_none=True)
    if not d.get("vitals"):
        d.pop("vitals", None)
    return d


async def _post(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    """One lane. Fail-open: any error -> {} so a partial encounter still works."""
    try:
        r = await client.post(f"{_base()}{path}", json=payload, headers=_headers())
        if r.status_code == 200:
            return r.json()
    except (httpx.HTTPError, ValueError):
        pass
    return {}


@router.post("/decision-support")
async def decision_support(body: DecisionSupportRequest, user: CurrentUser = Depends(get_current_user)):
    """Fire the three independent symptom lanes in parallel the moment symptoms are known."""
    if not _base() or not body.chief_complaints:
        return _empty()

    payload = _payload(body)
    async with httpx.AsyncClient(timeout=30) as client:
        ddx_r, inv_r, tx_r = await asyncio.gather(
            _post(client, "/api/rx/synthesize", payload),
            _post(client, "/api/rx/investigations", payload),
            _post(client, "/api/rx/treatment-fast", payload),
        )

    return {
        "available": bool(ddx_r or inv_r or tx_r),
        "differential_diagnosis": _ddx(ddx_r.get("differential_diagnosis")),
        "must_not_miss": [
            {"diagnosis": str(m.get("diagnosis", "")).strip()}
            for m in (ddx_r.get("must_not_miss") or []) if isinstance(m, dict) and m.get("diagnosis")
        ],
        "investigations": _investigations(inv_r.get("investigations")),
        "treatment": _treatment(tx_r.get("treatment_recommendations")),
        "confirmed": False,
    }


@router.post("/confirm")
async def confirm(body: ConfirmRequest, user: CurrentUser = Depends(get_current_user)):
    """Confirmation-locked synthesis for a chosen diagnosis: definitive Ix + Tx (+ sources)."""
    if not _base() or not body.confirmed_diagnoses:
        return {"available": False, "investigations": [], "treatment": [], "sources": [], "confirmed": True}

    payload = _payload(body)
    payload["confirmation_locked"] = True
    async with httpx.AsyncClient(timeout=40) as client:
        r = await _post(client, "/api/rx/synthesize", payload)

    return {
        "available": bool(r),
        "investigations": _investigations(r.get("suggested_investigations")),
        "treatment": _treatment(r.get("treatment_recommendations")),
        "sources": [
            {"book": str(s.get("book", "")).strip(), "page": s.get("page"),
             "snippet": str(s.get("snippet", "")).strip()[:300]}
            for s in (r.get("sources") or []) if isinstance(s, dict)
        ],
        "confirmed": True,
    }


# ------------------------------------------------------------------ normalisers
def _empty() -> dict:
    return {"available": False, "differential_diagnosis": [], "must_not_miss": [],
            "investigations": [], "treatment": [], "confirmed": False}


def _ddx(items: object) -> list[dict]:
    out = []
    for d in (items or []):
        if not isinstance(d, dict) or not d.get("diagnosis"):
            continue
        out.append({
            "diagnosis": str(d.get("diagnosis", "")).strip(),
            "likelihood": str(d.get("likelihood", "")).strip(),
            "reasoning": str(d.get("reasoning", "")).strip(),
            "icd10": str(d.get("icd10", "")).strip(),
        })
    return out


_URG = {"immediate", "urgent", "routine"}


def _investigations(items: object) -> list[dict]:
    out = []
    for i in (items or []):
        if not isinstance(i, dict):
            continue
        name = str(i.get("investigation", "")).strip()
        if not name:
            continue
        u = str(i.get("urgency", "")).strip().lower()
        out.append({
            "investigation": name,
            "urgency": u.title() if u in _URG else (i.get("urgency") or "Routine"),
            "rationale": str(i.get("rationale") or i.get("role") or "").strip(),
            "mnm_floor": bool(i.get("mnm_floor")),
        })
    return out


def _drug(d: dict) -> dict:
    return {
        "drug": str(d.get("drug", "")).strip(),
        "dose": str(d.get("dose", "")).strip(),
        "route": str(d.get("route", "")).strip(),
        "frequency": str(d.get("frequency", "")).strip(),
        "duration": str(d.get("duration", "")).strip(),
        "brands": [str(b).strip() for b in (d.get("brands") or []) if str(b).strip()][:4],
        "dose_needs_doctor": bool(d.get("dose_needs_doctor")),
        "dose_flag": str(d.get("dose_flag", "")).strip(),
    }


def _treatment(items: object) -> list[dict]:
    out = []
    for t in (items or []):
        if not isinstance(t, dict):
            continue
        first = [_drug(x) for x in (t.get("first_line") or []) if isinstance(x, dict) and x.get("drug")]
        nonpharm = [str(x).strip() for x in (t.get("non_pharmacological") or []) if str(x).strip()]
        if not first and not nonpharm:
            continue
        out.append({
            "diagnosis": str(t.get("diagnosis", "")).strip(),
            "first_line": first,
            "non_pharmacological": nonpharm,
        })
    return out
