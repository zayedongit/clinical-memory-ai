"""Async helpers over Supabase Auth + PostgREST.

Two access modes, and the difference is the whole security model:

* **user headers** — the caller's own JWT is forwarded to PostgREST, so every
  Row-Level Security policy is evaluated against *their* identity. This is the
  path for all patient data. If application code asks for another clinic's
  rows, the database returns nothing; the isolation does not depend on this
  code remembering a `where clinic_id = ...`.

* **service headers** — the service_role key, which bypasses RLS entirely.
  Used for exactly one thing: resolving `auth_uid -> users row` during
  authentication, because the caller cannot read `users` until we know which
  clinic they belong to. Every other use would silently discard tenant
  isolation, so new calls to it should be treated as a review flag.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings
from .metrics import Timer, registry

log = logging.getLogger("supabase")

# One pooled client for the process. Opening a connection pool per request (the
# previous `async with httpx.AsyncClient()` per call) meant a fresh TCP + TLS
# handshake for every database read — the single largest avoidable latency in
# the request path.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


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

    Uses /auth/v1/user rather than verifying the signature locally, so this
    stays correct whether the project signs with the legacy HMAC secret or
    with asymmetric keys, and so a revoked session stops working immediately.
    The cost is a network round trip per request, which is why the result is
    cached briefly upstream in `api/deps.py`.
    """
    with Timer("cma_supabase_seconds", {"op": "auth"}):
        try:
            resp = await get_client().get(
                f"{_base()}/auth/v1/user", headers=user_headers(token), timeout=10.0
            )
        except httpx.HTTPError as e:
            log.warning("auth_unreachable", extra={"extra_fields": {"error": type(e).__name__}})
            raise
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
    with Timer("cma_supabase_seconds", {"op": "rest"}):
        return await get_client().request(
            method, f"{_base()}/rest/v1/{path}", headers=hdrs, params=params, json=json
        )


async def rpc(name: str, *, headers: dict[str, str], args: dict[str, Any]) -> httpx.Response:
    """Call a Postgres function. Used for the operations that must be atomic."""
    with Timer("cma_supabase_seconds", {"op": "rpc"}):
        return await get_client().post(
            f"{_base()}/rest/v1/rpc/{name}",
            headers={**headers, "Content-Type": "application/json"},
            json=args,
        )


async def audit(
    *,
    clinic_id: str | None,
    actor_id: str | None,
    action: str,
    entity: str,
    entity_id: str | None,
    after: Any | None = None,
    before: Any | None = None,
) -> bool:
    """Write an audit entry via service role. Returns whether it landed.

    Auditing must not break the request path — a failed audit write should not
    lose a patient's note. But it must not be *invisible* either: the previous
    version swallowed every exception silently, so a broken audit trail looked
    exactly like a working one. Failures are now logged and counted, and the
    caller is told, so a route that considers the audit entry part of its
    contract can act on it.
    """
    try:
        resp = await rest(
            "POST", "audit_log", headers=service_headers(),
            json={
                "clinic_id": clinic_id, "actor_id": actor_id, "action": action,
                "entity": entity, "entity_id": entity_id, "before": before, "after": after,
            },
        )
    except httpx.HTTPError as e:
        registry.inc("cma_audit_write_failures_total", {"reason": "transport"})
        log.error("audit_write_failed",
                  extra={"extra_fields": {"action": action, "entity": entity,
                                          "error": type(e).__name__}})
        return False
    if resp.status_code not in (200, 201, 204):
        registry.inc("cma_audit_write_failures_total", {"reason": f"http_{resp.status_code}"})
        log.error("audit_write_rejected",
                  extra={"extra_fields": {"action": action, "entity": entity,
                                          "status": resp.status_code}})
        return False
    return True


async def ping() -> tuple[bool, str]:
    """Cheap readiness probe: can we reach PostgREST at all?"""
    try:
        r = await get_client().get(f"{_base()}/rest/v1/", headers=service_headers(), timeout=5.0)
    except httpx.HTTPError as e:
        return False, type(e).__name__
    # PostgREST answers the root with its OpenAPI document; any HTTP response
    # proves reachability and credential acceptance.
    return (r.status_code < 500), f"http_{r.status_code}"
