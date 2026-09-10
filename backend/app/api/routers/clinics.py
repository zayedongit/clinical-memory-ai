"""Onboarding: link a freshly-signed-up auth user to a new clinic.

This is the one endpoint a user calls *before* they belong to a clinic, so it
depends on a valid token (not get_current_user). It is idempotent: if the user
is already linked, it returns their existing clinic.
"""
from fastapi import APIRouter, Depends, HTTPException, status

from ...core import pgrst
from ...core.supabase import audit, rest, service_headers
from ...schemas import ClinicBootstrapRequest, ClinicBootstrapResponse
from ..deps import get_auth_uid, get_token, invalidate_identity_cache

router = APIRouter()


@router.post("/clinics/bootstrap", response_model=ClinicBootstrapResponse)
async def bootstrap(
    body: ClinicBootstrapRequest,
    # A dependency, not a plain Header parameter. FastAPI resolves dependencies
    # before validating the endpoint's own body, so an unauthenticated caller
    # gets 401 rather than a 422 that hands them the request schema.
    token: str = Depends(get_token),
) -> ClinicBootstrapResponse:
    auth_uid = await get_auth_uid(token)

    # Already linked? return existing (idempotent).
    existing = await rest(
        "GET",
        "users",
        headers=service_headers(),
        params={"auth_uid": pgrst.eq(auth_uid), "select": "id,clinic_id", "limit": "1"},
    )
    rows = existing.json() if existing.status_code == 200 else []
    if rows:
        return ClinicBootstrapResponse(clinic_id=rows[0]["clinic_id"], user_id=rows[0]["id"])

    # Create clinic (service role).
    c = await rest(
        "POST",
        "clinics",
        headers=service_headers(),
        json={"name": body.clinic_name},
        prefer="return=representation",
    )
    if c.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Clinic create failed: {c.text}")
    clinic_id = c.json()[0]["id"]

    # Create the linking user row.
    u = await rest(
        "POST",
        "users",
        headers=service_headers(),
        json={
            "clinic_id": clinic_id,
            "auth_uid": auth_uid,
            "name": body.user_name,
            "role": "doctor",
        },
        prefer="return=representation",
    )
    if u.status_code not in (200, 201):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"User create failed: {u.text}")
    user_id = u.json()[0]["id"]

    # The caller's previous request (before they had a clinic) may have been
    # cached as "not linked". Clear it so the very next request sees the link.
    invalidate_identity_cache()

    await audit(
        clinic_id=clinic_id,
        actor_id=user_id,
        action="bootstrap_clinic",
        entity="clinic",
        entity_id=clinic_id,
        after={"clinic_name": body.clinic_name},
    )
    return ClinicBootstrapResponse(clinic_id=clinic_id, user_id=user_id)
