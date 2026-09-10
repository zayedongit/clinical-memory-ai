"""Patient CRUD.

Every database call forwards the caller's own JWT, so Row-Level Security
enforces that a clinic only ever sees its own patients. No query in this file
filters by `clinic_id` in application code; that is deliberate, and it is what
makes a forgotten filter a non-event rather than a data breach.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ...core import pgrst
from ...core.supabase import audit, rest, user_headers
from ...schemas import (
    PatientCreateRequest,
    PatientListResponse,
    PatientResponse,
    PatientUpdateRequest,
)
from ..deps import CurrentUser, get_current_user

router = APIRouter(prefix="/patients")

_SELECT = "id,name,uhid,dob,gender,phone,address,pincode,city,state,height_cm,weight_kg,created_at"
_MAX_PAGE = 200


@router.get("", response_model=PatientListResponse)
async def list_patients(
    q: str | None = Query(default=None, max_length=120, description="search by name, phone or UHID"),
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(get_current_user),
) -> PatientListResponse:
    params: dict[str, str] = {
        "select": _SELECT,
        "order": "created_at.desc",
        "merged_into": "is.null",
        "deleted_at": "is.null",
        "limit": str(limit),
        "offset": str(offset),
    }
    if q and q.strip():
        # Previously this interpolated the raw query into the filter string,
        # so a `)` or `,` in a patient's name (or a probe) rewrote the filter
        # PostgREST parsed. pgrst.or_ilike quotes the value and neutralises
        # `%`/`_` so the search means what it says.
        params["or"] = pgrst.or_ilike(q.strip(), "name", "phone", "uhid")

    resp = await rest("GET", "patients", headers=user_headers(user.token), params=params,
                      prefer="count=exact")
    rows = resp.json() if resp.status_code == 200 else []
    return PatientListResponse(
        items=[PatientResponse(**r) for r in rows],
        total=_content_range_total(resp.headers.get("content-range"), len(rows)),
        limit=limit,
        offset=offset,
    )


def _content_range_total(header: str | None, fallback: int) -> int:
    """PostgREST reports `0-24/431` when asked for an exact count.

    The previous implementation returned `len(items)`, so "total" was really
    "how many fit on this page" — which made the number meaningless the moment
    pagination existed.
    """
    if not header or "/" not in header:
        return fallback
    tail = header.rsplit("/", 1)[1]
    return int(tail) if tail.isdigit() else fallback


@router.post("", response_model=PatientResponse, status_code=status.HTTP_201_CREATED)
async def create_patient(
    body: PatientCreateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> PatientResponse:
    payload = body.model_dump(mode="json", exclude_none=True)
    # RLS's WITH CHECK requires this to match the caller's clinic. It is set
    # from the token, never from the request body.
    payload["clinic_id"] = user.clinic_id

    resp = await rest("POST", "patients", headers=user_headers(user.token), json=payload,
                      prefer="return=representation")
    if resp.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Create failed: {resp.text[:300]}")
    row = resp.json()[0]

    # Audit `after` deliberately records which fields were set, not their
    # values: the audit log is queried and exported, and duplicating patient
    # demographics into it widens the blast radius of any access to it.
    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="create_patient",
                entity="patient", entity_id=row["id"],
                after={"fields": sorted(payload.keys()), "uhid": row.get("uhid")})
    return PatientResponse(**{k: row.get(k) for k in _SELECT.split(",")})


@router.get("/{patient_id}", response_model=PatientResponse)
async def get_patient(patient_id: str, user: CurrentUser = Depends(get_current_user)) -> PatientResponse:
    resp = await rest(
        "GET", "patients", headers=user_headers(user.token),
        params={"id": pgrst.eq(patient_id), "select": _SELECT, "deleted_at": "is.null", "limit": "1"},
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
    changes = body.model_dump(mode="json", exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

    resp = await rest("PATCH", "patients", headers=user_headers(user.token),
                      params={"id": pgrst.eq(patient_id), "deleted_at": "is.null"},
                      json=changes, prefer="return=representation")
    rows = resp.json() if resp.status_code in (200, 201) else []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Patient not found")

    await audit(clinic_id=user.clinic_id, actor_id=user.user_id, action="update_patient",
                entity="patient", entity_id=patient_id,
                after={"fields": sorted(changes.keys())})
    return PatientResponse(**{k: rows[0].get(k) for k in _SELECT.split(",")})
