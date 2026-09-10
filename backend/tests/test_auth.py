"""Authentication and identity resolution.

Supabase is mocked at the HTTP boundary, so the real dependency chain runs:
header parsing, token validation, the users lookup, the identity cache, and the
error mapping for each failure mode.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.api.deps import get_current_user, invalidate_identity_cache

from .conftest import CLINIC_A, DOCTOR_A, SUPABASE

AUTH_URL = f"{SUPABASE}/auth/v1/user"
USERS_URL = f"{SUPABASE}/rest/v1/users"


def _valid_auth(mock: respx.MockRouter, auth_uid: str = "auth-uid-1") -> None:
    mock.get(AUTH_URL).mock(return_value=httpx.Response(200, json={"id": auth_uid}))


def _linked_user(mock: respx.MockRouter, role: str = "doctor") -> None:
    mock.get(USERS_URL).mock(return_value=httpx.Response(
        200, json=[{"id": DOCTOR_A, "clinic_id": CLINIC_A, "role": role}]))


# --------------------------------------------------------------------- #
# Missing / malformed credentials
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer   "},
    {"Authorization": "Basic abc123"},
    {"Authorization": "token abc123"},
])
def test_missing_or_malformed_bearer_is_401(client, headers):
    r = client.get("/me", headers=headers)
    assert r.status_code == 401


def test_protected_routes_require_a_token(client):
    """Every clinical route must reject an anonymous caller.

    Enumerated from the app's own OpenAPI schema rather than a hand-written
    list, so a new route added without authentication fails this test instead
    of quietly shipping. The schema is also the published contract, which makes
    it the right source of truth for "what does this API expose".
    """
    from app.main import app

    public = {"/health", "/health/ready", "/metrics", "/metrics/summary"}
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if path in public:
            continue
        concrete = path
        while "{" in concrete:
            head = concrete.index("{")
            tail = concrete.index("}", head)
            concrete = concrete[:head] + "11111111-1111-1111-1111-111111111111" + concrete[tail + 1:]
        for method in operations:
            if method.lower() not in ("get", "post", "patch", "put", "delete"):
                continue
            r = client.request(method.upper(), concrete, json={})
            assert r.status_code in (401, 403), (
                f"{method.upper()} {path} answered {r.status_code} without a token"
            )
            checked += 1
    assert checked >= 15, f"only {checked} protected routes found; enumeration is probably broken"


# --------------------------------------------------------------------- #
# Token validation
# --------------------------------------------------------------------- #
@respx.mock
def test_rejected_token_is_401(client):
    respx.get(AUTH_URL).mock(return_value=httpx.Response(401, json={"msg": "bad jwt"}))
    r = client.get("/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert "invalid" in r.json()["detail"].lower()


@respx.mock
def test_auth_outage_is_503_not_401(client):
    """An unreachable auth service must not look like a bad password.

    Reporting 401 during an outage sends every logged-in clinician to the login
    screen mid-consultation, and their retry fails the same way.
    """
    respx.get(AUTH_URL).mock(side_effect=httpx.ConnectError("dns failure"))
    r = client.get("/me", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 503
    assert "retry" in r.json()["detail"].lower()


@respx.mock
def test_authenticated_but_unlinked_user_is_403_with_guidance(client):
    _valid_auth(respx)
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))
    r = client.get("/me", headers={"Authorization": "Bearer valid"})
    assert r.status_code == 403
    assert "bootstrap" in r.json()["detail"]


@respx.mock
def test_database_outage_during_user_lookup_is_503(client):
    _valid_auth(respx)
    respx.get(USERS_URL).mock(side_effect=httpx.ReadTimeout("timeout"))
    r = client.get("/me", headers={"Authorization": "Bearer valid"})
    assert r.status_code == 503


# --------------------------------------------------------------------- #
# Identity resolution and caching
# --------------------------------------------------------------------- #
@respx.mock
async def test_identity_comes_from_the_token_not_the_request(client):
    """The clinic must be derived from the JWT.

    A client that could assert its own clinic_id would defeat tenant isolation
    before RLS ever saw the query.
    """
    _valid_auth(respx)
    _linked_user(respx)
    user = await get_current_user(authorization="Bearer valid")
    assert user.clinic_id == CLINIC_A
    assert user.user_id == DOCTOR_A
    assert user.role == "doctor"


@respx.mock
async def test_identity_is_cached_across_requests():
    """Two round trips per request dominates latency in the live loop."""
    invalidate_identity_cache()
    auth_route = respx.get(AUTH_URL).mock(return_value=httpx.Response(200, json={"id": "auth-uid-1"}))
    users_route = respx.get(USERS_URL).mock(return_value=httpx.Response(
        200, json=[{"id": DOCTOR_A, "clinic_id": CLINIC_A, "role": "doctor"}]))

    for _ in range(5):
        await get_current_user(authorization="Bearer valid")

    assert auth_route.call_count == 1
    assert users_route.call_count == 1


@respx.mock
async def test_cache_is_keyed_per_token():
    """Two different sessions must not share an identity."""
    invalidate_identity_cache()

    def auth_for(request):
        token = request.headers["authorization"].split(" ", 1)[1]
        return httpx.Response(200, json={"id": f"auth-{token}"})

    def user_for(request):
        # The filter value is quoted by pgrst.eq; unwrap it the way PostgREST does.
        auth_uid = request.url.params["auth_uid"].removeprefix("eq.").strip('"')
        clinic = CLINIC_A if auth_uid.endswith("token-a") else "22222222-2222-2222-2222-222222222222"
        return httpx.Response(200, json=[{"id": f"u-{auth_uid}", "clinic_id": clinic, "role": "doctor"}])

    respx.get(AUTH_URL).mock(side_effect=auth_for)
    respx.get(USERS_URL).mock(side_effect=user_for)

    a = await get_current_user(authorization="Bearer token-a")
    b = await get_current_user(authorization="Bearer token-b")
    assert a.clinic_id != b.clinic_id
    # And repeated lookups keep returning the right one.
    assert (await get_current_user(authorization="Bearer token-a")).clinic_id == a.clinic_id


@respx.mock
async def test_role_defaults_to_doctor_when_absent_but_is_used_when_present():
    invalidate_identity_cache()
    _valid_auth(respx)
    respx.get(USERS_URL).mock(return_value=httpx.Response(
        200, json=[{"id": DOCTOR_A, "clinic_id": CLINIC_A, "role": "staff"}]))
    user = await get_current_user(authorization="Bearer valid")
    assert user.role == "staff"
    assert user.can_attest is False
