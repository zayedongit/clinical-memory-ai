"""Per-patient visit history + clinic consultation dashboard + delete.
RLS keeps everything clinic-scoped."""
from datetime import date as _date
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import CurrentUser, get_current_user
from ...core.supabase import audit, rest, user_headers

router = APIRouter()

_COMPLETED = ("approved", "completed")


@router.get("/consultations")
async def consultations(
    scope: str = "all", q: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Clinic-wide consultation log + headline stats (clinic dashboard)."""
    params = {
        "select": "id,started_at,approved_at,status,doctor_id,patient_id,"
                  "patients(name,uhid),soap_notes(assessment,subjective)",
        "order": "started_at.desc", "limit": "500",
        "deleted_at": "is.null",
    }
    if scope == "mine":
        params["doctor_id"] = f"eq.{user.user_id}"
    r = await rest("GET", "visits", headers=user_headers(user.token), params=params)
    rows = r.json() if r.status_code == 200 else []

    items = []
    for v in rows:
        p = v.get("patients") or {}
        notes = v.get("soap_notes") or []
        note = notes[0] if notes else {}
        items.append({
            "visit_id": v["id"],
            "patient_id": v.get("patient_id"),
            "patient_name": p.get("name"),
            "uhid": p.get("uhid"),
            "date": v.get("approved_at") or v.get("started_at"),
            "status": v.get("status"),
            "mine": v.get("doctor_id") == user.user_id,
            "assessment": (note.get("assessment") or note.get("subjective") or "").strip()[:120],
        })

    today = _date.today().isoformat()
    stats = {
        "total": len(items),
        "completed": sum(1 for i in items if i["status"] in _COMPLETED),
        "in_progress": sum(1 for i in items if i["status"] == "in_progress"),
        "today": sum(1 for i in items if i["status"] in _COMPLETED and (i["date"] or "").startswith(today)),
    }

    if q:
        ql = q.lower()
        items = [i for i in items if ql in (i["patient_name"] or "").lower()
                 or ql in (i["uhid"] or "").lower() or ql in (i["assessment"] or "").lower()]

    return {"items": items, "stats": stats}


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
                "deleted_at": "is.null", "order": "created_at.asc"},
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
        if problems:
            parts.append("Known problems: " + ", ".join(sorted(problems)) + ".")
        if meds:
            parts.append("Previously noted medications: " + ", ".join(sorted(meds)) + ".")
        if allergies:
            parts.append("Allergies: " + ", ".join(sorted(allergies)) + ".")
        if recurring:
            parts.append("Recurring symptoms: " + ", ".join(f"{x['term']} (x{x['count']})" for x in recurring[:5]) + ".")
        if since_last.get("new_symptoms"):
            parts.append("New since last visit: " + ", ".join(since_last["new_symptoms"]) + ".")

    return {
        "visit_count": len(notes),
        "problems": sorted(problems), "medications": sorted(meds), "allergies": sorted(allergies),
        "recurring_symptoms": recurring, "recent_visits": recent, "since_last": since_last,
        "context_text": " ".join(parts),
    }


@router.get("/patients/{patient_id}/memory")
async def patient_memory(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    """Longitudinal patient memory derived from the append-only clinical_facts
    store — shown automatically when a consult opens so the doctor sees the
    patient's story without lifting a finger. Zero input required."""
    r = await rest(
        "GET", "clinical_facts", headers=user_headers(user.token),
        params={"patient_id": f"eq.{patient_id}", "status": "eq.confirmed",
                "select": "fact_type,value,structured,visit_id,asserted_at",
                "order": "asserted_at.asc"},
    )
    facts = r.json() if r.status_code == 200 else []

    def _day(ts: str) -> str:
        return (ts or "")[:10]

    visits_order: list[str] = []
    for f in facts:
        vid = f.get("visit_id")
        if vid and vid not in visits_order:
            visits_order.append(vid)

    problems: dict[str, dict] = {}
    allergies: dict[str, str] = {}
    meds_by_visit: dict[str, set] = {}
    prob_by_visit: dict[str, set] = {}
    current_meds: dict[str, str] = {}
    trends: dict[str, list] = {}

    for f in facts:
        ft, val, st = f.get("fact_type"), (f.get("value") or "").strip(), f.get("structured") or {}
        vid, day = f.get("visit_id"), _day(f.get("asserted_at"))
        if not val:
            continue
        key = val.lower()
        if ft == "diagnosis":
            p = problems.setdefault(key, {"label": val, "count": 0, "first_seen": day, "last_seen": day})
            p["count"] += 1
            p["last_seen"] = day
            prob_by_visit.setdefault(vid, set()).add(key)
        elif ft == "allergy":
            allergies.setdefault(key, val)
        elif ft == "medication":
            meds_by_visit.setdefault(vid, set()).add(val)
            if st.get("context") == "prescribed":
                current_meds[val.lower()] = val    # last prescribed wins → current med list
        elif ft == "vital":
            metric = str(st.get("metric") or "").strip()
            reading = str(st.get("reading") or val).strip()
            if metric and reading:
                trends.setdefault(metric, []).append({"date": day, "value": reading})

    # "Since last visit": diff the two most recent visits.
    since_last: dict[str, list] = {}
    if len(visits_order) >= 2:
        last, prev = visits_order[-1], visits_order[-2]
        since_last = {
            "new_problems": sorted({problems[k]["label"] for k in (prob_by_visit.get(last, set()) - prob_by_visit.get(prev, set()))}),
            "new_medications": sorted(meds_by_visit.get(last, set()) - meds_by_visit.get(prev, set())),
            "stopped_medications": sorted(meds_by_visit.get(prev, set()) - meds_by_visit.get(last, set())),
        }

    return {
        "visit_count": len(visits_order),
        "problems": sorted(problems.values(), key=lambda p: (-p["count"], p["label"])),
        "allergies": sorted(allergies.values()),
        "current_medications": sorted(current_meds.values()),
        "trends": {k: v[-8:] for k, v in trends.items()},   # last 8 points per metric
        "since_last": since_last,
    }


@router.get("/patients/{patient_id}/visits")
async def list_visits(patient_id: str, user: CurrentUser = Depends(get_current_user)):
    r = await rest(
        "GET", "visits", headers=user_headers(user.token),
        params={
            "patient_id": f"eq.{patient_id}",
            "select": "id,started_at,approved_at,status,soap_notes(assessment,subjective)",
            "deleted_at": "is.null",
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
        params={"id": f"eq.{visit_id}", "limit": "1", "deleted_at": "is.null",
                "select": "id,started_at,approved_at,status,patient_id,"
                          "consent_given,consent_at,consent_method,"
                          "soap_notes(transcript,dialogue,subjective,objective,assessment,plan,"
                          "entities,follow_up_questions,prescription,clinical_considerations,vitals,wizard,"
                          "attested,attested_at,created_at)"},
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
        "consent_given": v.get("consent_given"),
        "consent_method": v.get("consent_method"),
        "note": notes[0] if notes else None,
    }


@router.delete("/visits/{visit_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_visit(visit_id: str, user: CurrentUser = Depends(get_current_user)):
    """Soft-delete. Clinical records are medico-legal documents — they are
    retained and hidden, never destroyed. The visit + its note are stamped
    with deleted_at/deleted_by and filtered out of every read path."""
    h = user_headers(user.token)
    stamp = {"deleted_at": datetime.now(timezone.utc).isoformat(), "deleted_by": user.user_id}
    r = await rest("PATCH", "visits", headers=h, params={"id": f"eq.{visit_id}"}, json=stamp)
    if r.status_code not in (200, 204):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Delete failed: {r.text[:200]}")
    await rest("PATCH", "soap_notes", headers=h, params={"visit_id": f"eq.{visit_id}"}, json=stamp)
    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="delete_visit",
                entity="visit", entity_id=visit_id)
    return None
