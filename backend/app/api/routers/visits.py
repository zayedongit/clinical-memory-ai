"""Per-patient visit history + delete. RLS keeps everything clinic-scoped."""
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import CurrentUser, get_current_user
from ...core.supabase import audit, rest, user_headers

router = APIRouter()


def _ents(note: dict, key: str) -> list[str]:
    e = note.get("entities") or {}
    return [str(x).strip() for x in (e.get(key) or []) if str(x).strip()]


@router.get("/patients/{patient_id}/summary")
async def patient_summary(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """Deterministic longitudinal summary across a patient's visits — the memory
    the scribe uses as context and the patient page shows as trends."""
    r = await rest(
        "GET", "soap_notes", headers=user_headers(user.token),
        params={"patient_id": f"eq.{patient_id}", "select": "created_at,assessment,entities",
                "order": "created_at.asc"},
    )
    notes = r.json() if r.status_code == 200 else []

    problems, meds, allergies = set(), set(), set()
    sym_counts: dict[str, int] = {}
    per_visit_syms, per_visit_meds = [], []
    for n in notes:
        problems.update(_ents(n, "diagnoses"))
        meds.update(_ents(n, "medications"))
        allergies.update(_ents(n, "allergies"))
        syms = {s.lower() for s in _ents(n, "symptoms")}
        per_visit_syms.append(syms)
        per_visit_meds.append({m.lower() for m in _ents(n, "medications")})
        for s in syms:
            sym_counts[s] = sym_counts.get(s, 0) + 1

    recurring = sorted(
        [{"term": s, "count": c} for s, c in sym_counts.items() if c >= 2],
        key=lambda x: -x["count"],
    )
    since_last: dict[str, list[str]] = {}
    if len(notes) >= 2:
        since_last = {
            "new_symptoms": sorted(per_visit_syms[-1] - per_visit_syms[-2]),
            "resolved_symptoms": sorted(per_visit_syms[-2] - per_visit_syms[-1]),
            "new_medications": sorted(per_visit_meds[-1] - per_visit_meds[-2]),
            "stopped_medications": sorted(per_visit_meds[-2] - per_visit_meds[-1]),
        }
    recent = [{"date": n["created_at"], "assessment": (n.get("assessment") or "").strip()[:200]}
              for n in notes[-3:]][::-1]

    parts: list[str] = []
    if notes:
        parts.append(f"{len(notes)} prior visit(s) on record.")
        if problems:  parts.append("Known problems: " + ", ".join(sorted(problems)) + ".")
        if meds:      parts.append("Previously noted medications: " + ", ".join(sorted(meds)) + ".")
        if allergies: parts.append("Allergies: " + ", ".join(sorted(allergies)) + ".")
        if recurring: parts.append("Recurring symptoms: " + ", ".join(f"{x['term']} (x{x['count']})" for x in recurring[:5]) + ".")
        if since_last.get("new_symptoms"): parts.append("New since last visit: " + ", ".join(since_last["new_symptoms"]) + ".")

    return {
        "visit_count": len(notes),
        "problems": sorted(problems), "medications": sorted(meds), "allergies": sorted(allergies),
        "recurring_symptoms": recurring, "recent_visits": recent, "since_last": since_last,
        "context_text": " ".join(parts),
    }


@router.get("/patients/{patient_id}/visits")
async def list_visits(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={
            "patient_id": f"eq.{patient_id}",
            "select": "id,started_at,approved_at,status,soap_notes(assessment,subjective)",
            "order": "approved_at.desc.nullslast,started_at.desc",
        },
    )
    rows = r.json() if r.status_code == 200 else []
    out = []
    for v in rows:
        note = (v.get("soap_notes") or [{}])
        note = note[0] if note else {}
        out.append({
            "id": v["id"],
            "date": v.get("approved_at") or v.get("started_at"),
            "status": v.get("status"),
            "summary": (note.get("assessment") or note.get("subjective") or "").strip()[:160],
        })
    return {"items": out, "total": len(out)}


@router.get("/visits/{visit_id}")
async def get_visit(visit_id: str, user: CurrentUser = Depends(get_current_user)):
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={"id": f"eq.{visit_id}", "limit": "1",
                "select": "id,started_at,approved_at,status,patient_id,"
                          "soap_notes(transcript,dialogue,subjective,objective,assessment,plan,"
                          "entities,follow_up_questions,created_at)"},
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
        "note": notes[0] if notes else None,
    }


@router.delete("/visits/{visit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_visit(visit_id: str, user: CurrentUser = Depends(get_current_user)):
    # soap_notes cascade-delete with the visit.
    r = await rest("DELETE", "visits", headers=user_headers(user.token),
                   params={"id": f"eq.{visit_id}"})
    if r.status_code not in (200, 204):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Delete failed: {r.text[:200]}")
    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="delete_visit",
                entity="visit", entity_id=visit_id)
    return None
