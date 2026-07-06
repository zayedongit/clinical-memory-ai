"""Patient CRUD. All DB calls go through the user's token, so Row-Level
Security enforces that a clinic only ever touches its own patients."""
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import CurrentUser, get_current_user
from ...core.supabase import audit, rest, user_headers
from ...schemas import (
    PatientCreateRequest,
    PatientListResponse,
    PatientResponse,
    PatientUpdateRequest,
)

router = APIRouter(prefix="/patients")

_SELECT = "id,name,dob,gender,phone,created_at"


@router.get("", response_model=PatientListResponse)
async def list_patients(
    q: str | None = Query(default=None, description="search by name"),
    user: CurrentUser = Depends(get_current_user),
) -> PatientListResponse:
    params = {"select": _SELECT, "order": "created_at.desc", "merged_into": "is.null"}
    if q:
        params["name"] = f"ilike.*{q}*"
    resp = await rest("GET", "patients", headers=user_headers(user.token), params=params)
    rows = resp.json() if resp.status_code == 200 else []
    items = [PatientResponse(**r) for r in rows]
    return PatientListResponse(items=items, total=len(items))


@router.post("", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: PatientCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PatientResponse:
    payload = body.model_dump(mode="json", exclude_none=True)
    payload["clinic_id"] = user.clinic_id  # RLS with-check requires this to match
    resp = await rest(
        "POST",
        "patients",
        headers=user_headers(user.token),
        json=payload,
        prefer="return=representation",
    )
    if resp.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Create failed: {resp.text}")
    row = resp.json()[0]
    await audit(
        clinic_id=user.clinic_id,
        actor_id=user.user_id,
        action="create_patient",
        entity="patient",
        entity_id=row["id"],
        after=payload,
    )
    return PatientResponse(**{k: row.get(k) for k in _SELECT.split(",")})


@router.get("/{patient_id}", response_model=PatientResponse)
async def get_patient(
    patient_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> PatientResponse:
    resp = await rest(
        "GET",
        "patients",
        headers=user_headers(user.token),
        params={"id": f"eq.{patient_id}", "select": _SELECT, "limit": "1"},
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient not found")
    return PatientResponse(**rows[0])


@router.patch("/{patient_id}", response_model=PatientResponse)
async def update_patient(
    patient_id: str,
    body: PatientUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PatientResponse:
    changes = body.model_dump(mode="json", exclude_none=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    resp = await rest(
        "PATCH",
        "patients",
        headers=user_headers(user.token),
        params={"id": f"eq.{patient_id}"},
        json=changes,
        prefer="return=representation",
    )
    rows = resp.json() if resp.status_code in (200, 201) else []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient not found")
    await audit(
        clinic_id=user.clinic_id,
        actor_id=user.user_id,
        action="update_patient",
        entity="patient",
        entity_id=patient_id,
        after=changes,
    )
    return PatientResponse(**{k: rows[0].get(k) for k in _SELECT.split(",")})
