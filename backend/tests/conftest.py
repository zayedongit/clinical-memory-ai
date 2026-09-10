"""Shared test fixtures.

Two ways of testing are used deliberately, and the split matters:

* **HTTP-level tests** run the real FastAPI app with Supabase mocked at the
  network boundary (respx). Everything between the request and the outbound
  HTTP call is the production code path — middleware, dependencies, validation,
  error translation. Only the third party is fake.

* **Database tests** (`tests/db/`) run the real migrations against a real
  PostgreSQL and exercise RLS, triggers and `finalize_visit()` as the database
  actually executes them. Mocking those would test a mock, and the whole point
  of putting isolation and attestation in the database is that they hold
  independently of the application.

Nothing here uses a real key, a real Supabase project, or real patient data.
"""
from __future__ import annotations

import os

import pytest

# Set before any app module imports, so Settings validates.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, get_current_user, invalidate_identity_cache
from app.core.budget import budget
from app.core.config import get_settings
from app.core.metrics import registry
from app.core.ratelimit import _buckets
from app.main import app

SUPABASE = "https://test.supabase.co"

CLINIC_A = "11111111-1111-1111-1111-111111111111"
CLINIC_B = "22222222-2222-2222-2222-222222222222"
DOCTOR_A = "aaaaaaaa-0000-0000-0000-00000000000a"
STAFF_A = "aaaaaaaa-0000-0000-0000-00000000000c"
PATIENT_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _user(role: str = "doctor", clinic_id: str = CLINIC_A, user_id: str = DOCTOR_A) -> CurrentUser:
    return CurrentUser(user_id=user_id, clinic_id=clinic_id, role=role,
                       auth_uid=f"auth-{user_id}", token="test-token")


@pytest.fixture(autouse=True)
def _clean_state():
    """Reset every piece of process-global state between tests.

    The rate limiter, the identity cache, the metrics registry and the budget
    are all module-level singletons. Without this, tests leak into each other
    and the failures are order-dependent, which is worse than no test at all.
    """
    get_settings.cache_clear()
    invalidate_identity_cache()
    _buckets.clear()
    registry.reset()
    budget.reset()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    """Unauthenticated client — the real dependency chain runs."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_doctor() -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: _user("doctor")
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def as_staff() -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: _user("staff", user_id=STAFF_A)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def settings_env(monkeypatch):
    """Set environment variables and rebuild Settings for one test."""

    def apply(**kwargs: str):
        for key, value in kwargs.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()
        return get_settings()

    return apply
