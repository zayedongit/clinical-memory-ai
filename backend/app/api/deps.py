"""Request dependencies: authenticate the bearer token and resolve the
caller's clinic and role.

Every protected route depends on `get_current_user`. The identity it returns
is derived from the token alone — never from the request body — so a client
cannot assert which clinic it belongs to.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException, status

from ..core import pgrst
from ..core.metrics import registry
from ..core.supabase import auth_get_user, rest, service_headers

# Resolving a caller costs two network round trips (validate token with
# Supabase Auth, then look up their users row). At the live consultation's
# request rate that dominates latency, so the resolved identity is cached for
# a short window.
#
# The trade-off, stated: a session revoked at Supabase stays usable here for
# up to TTL seconds. Thirty seconds is short enough that it is not a
# meaningful authorisation window and long enough to remove most of the
# round trips from a live consultation.
_CACHE_TTL_S = 30.0
_CACHE_MAX = 2048


@dataclass(frozen=True)
class CurrentUser:
    user_id: str
    clinic_id: str
    role: str
    auth_uid: str
    token: str

    @property
    def can_attest(self) -> bool:
        """Only a doctor may sign a clinical record. Clinic staff can prepare
        and draft, which is how a real front desk works, but attestation is
        the physician's legal act."""
        return self.role == "doctor"


class _IdentityCache:
    def __init__(self) -> None:
        self._store: OrderedDict[str, tuple[float, CurrentUser]] = OrderedDict()

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token: str) -> CurrentUser | None:
        k = self.key(token)
        hit = self._store.get(k)
        if hit is None:
            return None
        expires, user = hit
        if expires < time.monotonic():
            self._store.pop(k, None)
            return None
        self._store.move_to_end(k)
        return user

    def put(self, token: str, user: CurrentUser) -> None:
        k = self.key(token)
        self._store[k] = (time.monotonic() + _CACHE_TTL_S, user)
        self._store.move_to_end(k)
        while len(self._store) > _CACHE_MAX:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()


_identities = _IdentityCache()


async def get_token(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return token


async def get_auth_uid(token: str) -> str:
    """Return the Supabase auth user id for a valid token, else 401."""
    try:
        user = await auth_get_user(token)
    except httpx.HTTPError:
        # Auth is unreachable. This is not the caller's fault and must not be
        # reported as "your token is invalid" — that sends users to re-login
        # during an outage that a retry would survive.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Authentication service is unreachable. Please retry.",
        ) from None
    if not user or "id" not in user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user["id"]


async def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    token = await get_token(authorization)

    cached = _identities.get(token)
    if cached is not None:
        registry.inc("cma_identity_cache_total", {"result": "hit"})
        return cached
    registry.inc("cma_identity_cache_total", {"result": "miss"})

    auth_uid = await get_auth_uid(token)

    # `users` is RLS-protected and the caller cannot read it until we know
    # their clinic, so this one lookup uses the service role.
    try:
        resp = await rest(
            "GET", "users", headers=service_headers(),
            params={"auth_uid": pgrst.eq(auth_uid), "select": "id,clinic_id,role", "limit": "1"},
        )
    except httpx.HTTPError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Database is unreachable. Please retry."
        ) from None

    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "User is authenticated but not linked to a clinic. Call /clinics/bootstrap first.",
        )
    row = rows[0]
    user = CurrentUser(
        user_id=row["id"], clinic_id=row["clinic_id"], role=row.get("role") or "doctor",
        auth_uid=auth_uid, token=token,
    )
    _identities.put(token, user)
    return user


def invalidate_identity_cache() -> None:
    """Called when a user's clinic linkage changes, and by tests."""
    _identities.clear()
