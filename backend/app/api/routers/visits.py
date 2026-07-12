"""Per-patient visit history + delete. RLS keeps everything clinic-scoped."""
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import CurrentUser, get_current_user
from ...core.supabase import audit, rest, user_headers

router = APIRouter()


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
