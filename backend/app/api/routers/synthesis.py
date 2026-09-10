"""Proxy to the external Clinical Synthesis API.

**This is not our clinical brain.** The differential diagnoses, investigations
and treatment recommendations returned by these endpoints are produced by a
separate, externally maintained service that this project did not build. What
lives here is the integration: request shaping, response normalisation, failure
containment, and the rule that the browser never talks to it directly.

Why the proxy exists at all: the upstream base URL is a shared secret and the
service is unauthenticated on a shared budget, so exposing it to the browser
would hand anyone who opened devtools an open endpoint on someone else's bill.

Endpoints:
  POST /synthesis/decision-support  symptoms -> DDx + must-not-miss +
                                    investigations + empiric treatment
                                    (three independent lanes, fired in parallel)
  POST /synthesis/confirm           a chosen diagnosis -> definitive Ix + Tx + sources

**Fail-open, loudly.** An upstream error returns empty lists with
``available: false`` rather than a 5xx, because a decision-support outage must
not stop a physician documenting a consultation. The client is required to show
an explicit "unavailable" banner: the dangerous failure mode is a doctor reading
an empty differential as "nothing to worry about". Every failure is counted, so
"fails open" does not quietly become "always empty".

Everything returned is physician-review-only. The physician decides.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ...core.config import get_settings
from ...core.metrics import Timer, registry
from ..deps import CurrentUser, get_current_user

log = logging.getLogger("synthesis")
router = APIRouter(prefix="/synthesis")


class DecisionSupportRequest(BaseModel):
    chief_complaints: list[str] = Field(max_length=20)
    age: str | None = Field(default=None, max_length=16)
    gender: str | None = Field(default=None, max_length=32)
    patient_weight: str | None = Field(default=None, max_length=16)
    duration: str | None = Field(default=None, max_length=64)
    vitals: dict | None = None


class ConfirmRequest(BaseModel):
    chief_complaints: list[str] = Field(max_length=20)
    confirmed_diagnoses: list[str] = Field(max_length=5)
    age: str | None = Field(default=None, max_length=16)
    gender: str | None = Field(default=None, max_length=32)
    patient_weight: str | None = Field(default=None, max_length=16)
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
    """One lane. Fails open to {} so a partial encounter still works — but the
    failure is logged and counted, because a silent fail-open is
    indistinguishable from an upstream that simply has nothing to say."""
    lane = path.rsplit("/", 1)[-1]
    labels = {"capability": "decision_support", "provider": "synthesis", "lane": lane}
    try:
        with Timer("cma_ai_call_seconds", labels):
            r = await client.post(f"{_base()}{path}", json=payload, headers=_headers())
        if r.status_code == 200:
            registry.inc("cma_ai_calls_total", {**labels, "outcome": "ok"})
            return r.json()
        registry.inc("cma_ai_calls_total", {**labels, "outcome": f"http_{r.status_code}"})
        log.warning("synthesis_lane_failed",
                    extra={"extra_fields": {"lane": lane, "status": r.status_code}})
    except (httpx.HTTPError, ValueError) as e:
        registry.inc("cma_ai_calls_total", {**labels, "outcome": "transport_error"})
        log.warning("synthesis_lane_error",
                    extra={"extra_fields": {"lane": lane, "error": type(e).__name__}})
    return {}


@router.post("/decision-support")
async def decision_support(body: DecisionSupportRequest, user: CurrentUser = Depends(get_current_user)):
    """Fire the three independent symptom lanes in parallel the moment symptoms are known."""
    if not _base() or not body.chief_complaints:
        return _empty()

    payload = _payload(body)
    async with httpx.AsyncClient(timeout=get_settings().synthesis_timeout_s) as client:
        ddx_r, inv_r, tx_r = await asyncio.gather(
            _post(client, "/api/rx/synthesize", payload),
            _post(client, "/api/rx/investigations", payload),
            _post(client, "/api/rx/treatment-fast", payload),
        )

    available = bool(ddx_r or inv_r or tx_r)
    registry.inc("cma_decision_support_total", {"available": str(available).lower()})
    return {
        "available": available,
        "source": "external Clinical Synthesis API (not built by this project)",
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
    async with httpx.AsyncClient(timeout=get_settings().synthesis_timeout_s + 10) as client:
        r = await _post(client, "/api/rx/synthesize", payload)

    return {
        "available": bool(r),
        "source": "external Clinical Synthesis API (not built by this project)",
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


def _strings(value: object, limit: int | None = None) -> list[str]:
    """`str(None)` is `"None"`, so nulls in an upstream array must be dropped
    rather than stringified into the clinician's screen."""
    items = [x.strip() for x in (value or []) if isinstance(x, str) and x.strip()]
    return items[:limit] if limit else items


def _drug(d: dict) -> dict:
    return {
        "drug": str(d.get("drug", "")).strip(),
        "dose": str(d.get("dose", "")).strip(),
        "route": str(d.get("route", "")).strip(),
        "frequency": str(d.get("frequency", "")).strip(),
        "duration": str(d.get("duration", "")).strip(),
        "brands": _strings(d.get("brands"), 4),
        "dose_needs_doctor": bool(d.get("dose_needs_doctor")),
        "dose_flag": str(d.get("dose_flag", "")).strip(),
    }


def _treatment(items: object) -> list[dict]:
    out = []
    for t in (items or []):
        if not isinstance(t, dict):
            continue
        first = [_drug(x) for x in (t.get("first_line") or []) if isinstance(x, dict) and x.get("drug")]
        nonpharm = _strings(t.get("non_pharmacological"))
        if not first and not nonpharm:
            continue
        out.append({
            "diagnosis": str(t.get("diagnosis", "")).strip(),
            "first_line": first,
            "non_pharmacological": nonpharm,
        })
    return out
