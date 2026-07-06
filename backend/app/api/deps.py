"""Request dependencies: extract the bearer token, validate it, and resolve
the caller's clinic from the users table."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from ..core.supabase import auth_get_user, rest, service_headers


@dataclass
class CurrentUser:
    user_id: str
    clinic_id: str
    role: str
    auth_uid: str
    token: str


async def get_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def get_auth_uid(token: str) -> str:
    """Return the Supabase auth user id for a valid token, else 401."""
    user = await auth_get_user(token)
    if not user or "id" not in user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user["id"]


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    token = await get_token(authorization)
    auth_uid = await get_auth_uid(token)

    # Resolve the clinic mapping via service role (users has RLS).
    resp = await rest(
        "GET",
        "users",
        headers=service_headers(),
        params={"auth_uid": f"eq.{auth_uid}", "select": "id,clinic_id,role", "limit": "1"},
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "User is authenticated but not linked to a clinic. Call /clinics/bootstrap first.",
        )
    row = rows[0]
    return CurrentUser(
        user_id=row["id"],
        clinic_id=row["clinic_id"],
        role=row.get("role", "doctor"),
        auth_uid=auth_uid,
        token=token,
    )
