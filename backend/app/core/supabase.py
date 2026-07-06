"""Thin async helpers over Supabase Auth + PostgREST.

Two access modes:
  * user headers  -> requests run as the logged-in user, so Row-Level
                     Security applies (used for patient reads/writes).
  * service headers -> service_role key, bypasses RLS (used only for the
                     users lookup during auth and for audit-log writes).
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings


def _base() -> str:
    return get_settings().supabase_url.rstrip("/")


def user_headers(token: str) -> dict[str, str]:
    s = get_settings()
    return {"apikey": s.supabase_anon_key, "Authorization": f"Bearer {token}"}


def service_headers() -> dict[str, str]:
    key = get_settings().supabase_service_role_key
    return {"apikey": key, "Authorization": f"Bearer {key}"}


async def auth_get_user(token: str) -> dict[str, Any] | None:
    """Validate a JWT with Supabase Auth; return the user object or None.

    Uses the /auth/v1/user endpoint so verification is correct regardless of
    whether the project signs JWTs with the legacy secret or asymmetric keys.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_base()}/auth/v1/user", headers=user_headers(token))
    return resp.json() if resp.status_code == 200 else None


async def rest(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    prefer: str | None = None,
) -> httpx.Response:
    """Call PostgREST at /rest/v1/<path>."""
    hdrs = {**headers, "Content-Type": "application/json"}
    if prefer:
        hdrs["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15) as client:
        return await client.request(
            method, f"{_base()}/rest/v1/{path}", headers=hdrs, params=params, json=json
        )


async def audit(
    *,
    clinic_id: str | None,
    actor_id: str | None,
    action: str,
    entity: str,
    entity_id: str | None,
    after: Any | None = None,
) -> None:
    """Best-effort immutable audit write via service role (bypasses RLS)."""
    try:
        await rest(
            "POST",
            "audit_log",
            headers=service_headers(),
            json={
                "clinic_id": clinic_id,
                "actor_id": actor_id,
                "action": action,
                "entity": entity,
                "entity_id": entity_id,
                "after": after,
            },
        )
    except Exception:  # auditing must never break the request path
        pass
