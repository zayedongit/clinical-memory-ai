"""Visit history, the clinic dashboard, longitudinal patient memory, and the
soft-delete / amendment paths.

Every read goes through the caller's own token, so Row-Level Security decides
what exists. Nothing here filters by clinic in application code, on purpose:
the moment isolation depends on remembering a `where` clause, it is one
forgotten clause away from a breach.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ...clinical import longitudinal
from ...core import pgrst
from ...core.supabase import audit, rest, rpc, user_headers
from ..deps import CurrentUser, get_current_user

log = logging.getLogger("visits")
router = APIRouter()

_COMPLETED = ("approved", "completed")


@router.get("/consultations")
async def consultations(
    scope: str = Query(default="all", pattern="^(all|mine)$"),
    q: str | None = Query(default=None, max_length=120),
    user: CurrentUser = Depends(get_current_user),
):
    """Clinic-wide consultation log plus headline stats."""
    params = {
        "select": "id,started_at,approved_at,status,doctor_id,patient_id,version,"
                  "patients(name,uhid),soap_notes(assessment,subjective)",
        "order": "started_at.desc", "limit": "500",
        "deleted_at": "is.null",
    }
    if scope == "mine":
        params["doctor_id"] = pgrst.eq(user.user_id)

    r = await rest("GET", "visits", headers=user_headers(user.token), params=params)
    rows = r.json() if r.status_code == 200 else []

    items = []
    for v in rows:
        patient = v.get("patients") or {}
        notes = v.get("soap_notes") or []
        note = notes[0] if notes else {}
        items.append({
            "visit_id": v["id"],
            "patient_id": v.get("patient_id"),
            "patient_name": patient.get("name"),
            "uhid": patient.get("uhid"),
            "date": v.get("approved_at") or v.get("started_at"),
            "status": v.get("status"),
            "version": v.get("version"),
            "mine": v.get("doctor_id") == user.user_id,
            "assessment": (note.get("assessment") or note.get("subjective") or "").strip()[:120],
        })

    today = date.today().isoformat()
    stats = {
        "total": len(items),
        "completed": sum(1 for i in items if i["status"] in _COMPLETED),
        "in_progress": sum(1 for i in items if i["status"] in ("in_progress", "draft")),
        "today": sum(1 for i in items
                     if i["status"] in _COMPLETED and (i["date"] or "").startswith(today)),
    }

    if q:
        needle = q.lower()
        items = [i for i in items
                 if needle in (i["patient_name"] or "").lower()
                 or needle in (i["uhid"] or "").lower()
                 or needle in (i["assessment"] or "").lower()]

    return {"items": items, "stats": stats}


# --------------------------------------------------------------------- #
# Longitudinal memory
# --------------------------------------------------------------------- #
async def _confirmed_facts(patient_id: str, token: str) -> list[dict]:
    r = await rest(
        "GET", "clinical_facts", headers=user_headers(token),
        params={
            "patient_id": pgrst.eq(patient_id),
            "status": "eq.confirmed",
            "select": "fact_type,value,structured,visit_id,asserted_at,status,clinical_status",
            "order": "asserted_at.asc",
            "limit": "2000",
        },
    )
    return r.json() if r.status_code == 200 else []


@router.get("/patients/{patient_id}/memory")
async def patient_memory(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """The patient's story, assembled from the append-only fact store.

    Shown automatically when a consult opens so the physician sees continuity
    without asking for it. Only `confirmed` facts are used: a fact that a
    physician never signed is not memory.
    """
    facts = await _confirmed_facts(patient_id, user.token)

    visits_order: list[str] = []
    problems: dict[str, dict] = {}
    allergies: dict[str, str] = {}
    documented_no_allergies = False
    current_meds: dict[str, str] = {}

    for f in facts:
        visit_id = f.get("visit_id")
        if visit_id and visit_id not in visits_order:
            visits_order.append(visit_id)

        fact_type = f.get("fact_type")
        value = (f.get("value") or "").strip()
        structured = f.get("structured") or {}
        if not value:
            continue
        key = value.lower()
        day = (f.get("asserted_at") or "")[:10]

        if fact_type == "diagnosis":
            p = problems.setdefault(key, {"label": value, "count": 0, "first_seen": day,
                                          "last_seen": day,
                                          "clinical_status": f.get("clinical_status") or "current"})
            p["count"] += 1
            p["last_seen"] = day
            p["clinical_status"] = f.get("clinical_status") or p["clinical_status"]
        elif fact_type == "allergy":
            if structured.get("documented_negative"):
                documented_no_allergies = True
            else:
                allergies.setdefault(key, value)
        elif fact_type == "medication" and structured.get("context") == "prescribed":
            current_meds[key] = value

    analytics = longitudinal.build(facts)

    return {
        "visit_count": len(visits_order),
        "problems": sorted(problems.values(), key=lambda p: (-p["count"], p["label"])),
        "active_problems": [p for p in problems.values() if p["clinical_status"] == "current"],
        "allergies": sorted(allergies.values()),
        # "No known drug allergies" is a documented answer, not an empty one.
        # The UI shows it differently from "we never asked", which is the whole
        # point of recording a negative.
        "allergy_status": (
            "documented_none" if documented_no_allergies and not allergies
            else "documented" if allergies else "not_recorded"
        ),
        "current_medications": sorted(current_meds.values()),
        "medication_changes": analytics["medications"],
        "trends": analytics["trends"],
        "flagged_metrics": analytics["flagged_metrics"],
        "recurring_symptoms": analytics["recurring_symptoms"],
        "unresolved": analytics["unresolved"],
        "since_last": _since_last(facts, visits_order),
        "method": analytics["method"],
        "disclaimer": analytics["disclaimer"],
    }


@router.get("/patients/{patient_id}/summary")
async def patient_summary(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """A compact, deterministic history summary.

    This is what gets handed to the AI lanes as `patient_context`, so it is
    kept short and factual on purpose: a long context costs tokens on every
    12-second live refresh, and a speculative one would let last visit's
    guesses leak into this visit's note as though they were established.
    """
    facts = await _confirmed_facts(patient_id, user.token)
    analytics = longitudinal.build(facts)

    problems, allergies, meds, none_known = [], [], [], False
    visits: list[str] = []
    for f in facts:
        value = (f.get("value") or "").strip()
        structured = f.get("structured") or {}
        if f.get("visit_id") and f["visit_id"] not in visits:
            visits.append(f["visit_id"])
        if not value:
            continue
        if f.get("fact_type") == "diagnosis" and value not in problems:
            problems.append(value)
        elif f.get("fact_type") == "allergy":
            if structured.get("documented_negative"):
                none_known = True
            elif value not in allergies:
                allergies.append(value)
        elif f.get("fact_type") == "medication" and structured.get("context") == "prescribed" \
                and value not in meds:
            meds.append(value)

    recurring = analytics["recurring_symptoms"][:5]
    parts = []
    if visits:
        parts.append(f"{len(visits)} prior visit(s) on record.")
    if allergies:
        parts.append("Allergies: " + ", ".join(sorted(allergies)) + ".")
    elif none_known:
        parts.append("No known drug allergies (documented).")
    if problems:
        parts.append("Known problems: " + ", ".join(sorted(problems)) + ".")
    if meds:
        parts.append("Current medications: " + ", ".join(sorted(meds)) + ".")
    if recurring:
        parts.append("Recurring symptoms: "
                     + ", ".join(f"{x['term']} (x{x['occurrences']})" for x in recurring) + ".")
    for metric, t in analytics["trends"].items():
        if t["significant"]:
            parts.append(f"{metric.upper()} is {t['direction']} across visits (p={t['p_value']}).")

    return {
        "visit_count": len(visits),
        "problems": sorted(problems),
        "medications": sorted(meds),
        "allergies": sorted(allergies),
        "allergy_status": ("documented_none" if none_known and not allergies
                           else "documented" if allergies else "not_recorded"),
        "recurring_symptoms": recurring,
        "flagged_metrics": analytics["flagged_metrics"],
        "since_last": _since_last(facts, visits),
        "context_text": " ".join(parts),
    }


@router.get("/patients/{patient_id}/analytics")
async def patient_analytics(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """The full longitudinal analysis: trend tests, step detection, recurrence,
    medication churn and unresolved problems, with the method stated."""
    facts = await _confirmed_facts(patient_id, user.token)
    if not facts:
        return {"available": False, "reason": "No confirmed clinical facts recorded yet.",
                "fact_count": 0}
    result = longitudinal.build(facts)
    result["available"] = True
    result["fact_count"] = len(facts)
    return result


def _since_last(facts: list[dict], visits_order: list[str]) -> dict:
    """What changed between the two most recent visits."""
    if len(visits_order) < 2:
        return {}
    last, prev = visits_order[-1], visits_order[-2]

    def collect(visit_id: str, fact_type: str, context: str | None = None) -> set[str]:
        out = set()
        for f in facts:
            if f.get("visit_id") != visit_id or f.get("fact_type") != fact_type:
                continue
            if context and (f.get("structured") or {}).get("context") != context:
                continue
            value = (f.get("value") or "").strip()
            if value:
                out.add(value)
        return out

    last_meds = collect(last, "medication", "prescribed")
    prev_meds = collect(prev, "medication", "prescribed")
    return {
        "new_problems": sorted(collect(last, "diagnosis") - collect(prev, "diagnosis")),
        "new_symptoms": sorted(collect(last, "symptom") - collect(prev, "symptom")),
        "resolved_symptoms": sorted(collect(prev, "symptom") - collect(last, "symptom")),
        "new_medications": sorted(last_meds - prev_meds),
        "stopped_medications": sorted(prev_meds - last_meds),
    }


@router.get("/patients/{patient_id}/last-visit")
async def last_visit(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """The most recent signed prescription — powers 'same as last visit'
    carry-forward for chronic patients. The physician always confirms or edits."""
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={
            "patient_id": pgrst.eq(patient_id),
            "status": f"in.({','.join(_COMPLETED)})",
            "deleted_at": "is.null",
            "select": "id,approved_at,started_at,soap_notes(prescription,assessment)",
            "order": "approved_at.desc.nullslast,started_at.desc",
            "limit": "1",
        },
    )
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        return {"found": False, "visit_date": None, "assessment": "", "prescription": []}
    v = rows[0]
    notes = v.get("soap_notes") or []
    note = notes[0] if notes else {}
    rx = note.get("prescription") or []
    return {
        "found": bool(rx),
        "visit_date": v.get("approved_at") or v.get("started_at"),
        "assessment": (note.get("assessment") or "").strip()[:160],
        "prescription": rx,
    }


@router.get("/patients/{patient_id}/visits")
async def list_visits(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={
            "patient_id": pgrst.eq(patient_id),
            "select": "id,started_at,approved_at,status,version,soap_notes(assessment,subjective)",
            "deleted_at": "is.null",
            "order": "approved_at.desc.nullslast,started_at.desc",
        },
    )
    rows = r.json() if r.status_code == 200 else []
    items = []
    for v in rows:
        notes = v.get("soap_notes") or []
        note = notes[0] if notes else {}
        items.append({
            "id": v["id"],
            "date": v.get("approved_at") or v.get("started_at"),
            "status": v.get("status"),
            "version": v.get("version"),
            "summary": (note.get("assessment") or note.get("subjective") or "").strip()[:160],
        })
    return {"items": items, "total": len(items)}


@router.get("/visits/{visit_id}")
async def get_visit(visit_id: str, user: CurrentUser = Depends(get_current_user)):
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={"id": pgrst.eq(visit_id), "limit": "1", "deleted_at": "is.null",
                "select": "id,started_at,approved_at,status,patient_id,version,"
                          "consent_given,consent_at,consent_method,"
                          "soap_notes(transcript,dialogue,subjective,objective,assessment,plan,"
                          "entities,follow_up_questions,prescription,clinical_considerations,"
                          "vitals,wizard,sign_off,amendments,attested,attested_at,created_at)"},
    )
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visit not found")
    v = rows[0]
    notes = v.get("soap_notes") or []
    return {
        "id": v["id"],
        "date": v.get("approved_at") or v.get("started_at"),
        "status": v.get("status"),
        "patient_id": v.get("patient_id"),
        # Returned so the client can send it back on save. This is the read
        # half of the optimistic lock: finalize_visit() rejects a write whose
        # expected_version no longer matches.
        "version": v.get("version"),
        "consent_given": v.get("consent_given"),
        "consent_method": v.get("consent_method"),
        "note": notes[0] if notes else None,
    }


class AmendRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    text: str = Field(min_length=3, max_length=5_000)


@router.post("/visits/{visit_id}/amend", status_code=status.HTTP_201_CREATED)
async def amend(visit_id: str, body: AmendRequest, user: CurrentUser = Depends(get_current_user)):
    """Correct a signed note.

    A signed clinical note is a medico-legal document: the text the physician
    attested is what they attested, and rewriting it destroys the record of
    what was decided at the time. Corrections are therefore appended with an
    author, a timestamp and a reason, exactly as a paper chart is amended.
    """
    if not user.can_attest:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Role '{user.role}' may not amend a clinical note.")
    resp = await rpc("amend_visit_note", headers=user_headers(user.token),
                     args={"p_visit_id": visit_id, "p_reason": body.reason, "p_text": body.text})
    if resp.status_code not in (200, 201):
        from .scribe import _translate_db_error
        raise _translate_db_error(resp)
    return resp.json()


class DeleteRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/visits/{visit_id}/delete", status_code=status.HTTP_200_OK)
async def delete_visit(visit_id: str, body: DeleteRequest,
                       user: CurrentUser = Depends(get_current_user)):
    """Soft-delete a visit, with a required reason.

    Clinical records are retained and hidden, never destroyed — the database
    revokes DELETE on these tables outright. A reason is mandatory: removing a
    signed record from view without one is exactly the action an audit needs to
    be able to reconstruct.

    Modelled as POST rather than DELETE because it carries a body and because
    it is not, in fact, a deletion.
    """
    if not user.can_attest:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Role '{user.role}' may not remove a clinical record.")

    headers = user_headers(user.token)
    stamp = {"deleted_at": datetime.now(UTC).isoformat(), "deleted_by": user.user_id}

    v = await rest("PATCH", "visits", headers=headers, prefer="return=representation",
                   params={"id": pgrst.eq(visit_id), "deleted_at": "is.null"},
                   json={**stamp, "delete_reason": body.reason})
    if v.status_code not in (200, 204):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Delete failed: {v.text[:200]}")
    if v.status_code == 200 and not v.json():
        # Zero rows matched: either the visit is not ours (RLS), or it is
        # already deleted. Reporting success would be a lie either way — this
        # is the exact silent-no-op class of bug that the missing soap_notes
        # UPDATE policy caused before.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Visit not found or already removed")

    n = await rest("PATCH", "soap_notes", headers=headers,
                   params={"visit_id": pgrst.eq(visit_id)}, json=stamp)
    if n.status_code not in (200, 204):
        log.error("note_soft_delete_failed",
                  extra={"extra_fields": {"visit_id": visit_id, "status": n.status_code}})

    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="delete_visit",
                entity="visit", entity_id=visit_id, after={"reason": body.reason})
    return {"visit_id": visit_id, "deleted": True}
