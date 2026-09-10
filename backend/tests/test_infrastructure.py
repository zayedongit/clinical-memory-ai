"""Rate limiting, spend control, metrics, logging redaction, and configuration.

The cross-cutting machinery. Each of these has a specific bug it exists to
prevent, named in the test.
"""
from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.core.budget import budget
from app.core.metrics import Registry, registry
from app.core.observability import _redact, route_template
from app.core.ratelimit import _buckets, _caller_key, route_group

from .conftest import SUPABASE


# ===================================================================== #
# Rate limiting
# ===================================================================== #
def test_ai_routes_get_the_stricter_budget():
    assert route_group("/scribe/transcribe") == "ai"
    assert route_group("/scribe/live") == "ai"
    assert route_group("/synthesis/decision-support") == "ai"
    assert route_group("/patients") == "default"
    # /scribe/save writes to the database and costs nothing at a provider, so
    # it must not compete for the AI budget with the live transcription loop.
    assert route_group("/scribe/save") == "default"


def test_health_and_metrics_are_never_rate_limited():
    """An orchestrator that gets a 429 from /health restarts a server that was
    merely busy."""
    from app.core.ratelimit import _EXEMPT

    for path in ("/health", "/health/ready", "/metrics"):
        assert path.startswith(_EXEMPT)


def test_limit_is_enforced_then_releases(as_doctor, settings_env):
    settings_env(RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT_PER_MIN="3")
    _buckets.clear()

    codes = [as_doctor.get("/health/ready").status_code for _ in range(5)]
    assert 429 not in codes, "exempt paths must never be limited"

    with respx.mock:
        respx.get(f"{SUPABASE}/rest/v1/patients").mock(return_value=httpx.Response(200, json=[]))
        statuses = [as_doctor.get("/patients").status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429


def test_rate_limited_response_tells_the_client_when_to_retry(as_doctor, settings_env):
    settings_env(RATE_LIMIT_ENABLED="true", RATE_LIMIT_DEFAULT_PER_MIN="1")
    _buckets.clear()
    with respx.mock:
        respx.get(f"{SUPABASE}/rest/v1/patients").mock(return_value=httpx.Response(200, json=[]))
        as_doctor.get("/patients")
        blocked = as_doctor.get("/patients")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert "retry_after_seconds" in blocked.json()


def test_authenticated_callers_are_keyed_separately_not_by_shared_ip():
    """Behind a proxy an entire clinic shares one address; keying on IP alone
    let one busy consultation rate-limit the whole building."""

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers
            self.client = type("C", (), {"host": "10.0.0.1"})()

    a = _caller_key(FakeRequest({"authorization": "Bearer token-doctor-a"}))
    b = _caller_key(FakeRequest({"authorization": "Bearer token-doctor-b"}))
    assert a != b
    assert a.startswith("u:")
    # The token itself is never the key — only a hash of it.
    assert "token-doctor-a" not in a


def test_unauthenticated_callers_fall_back_to_ip():
    class FakeRequest:
        headers = {"x-forwarded-for": "203.0.113.9, 10.0.0.1"}
        client = None

    assert _caller_key(FakeRequest()) == "ip:203.0.113.9"


def test_bucket_store_is_bounded():
    """An unbounded per-IP dictionary turns the limiter itself into a
    memory-exhaustion vector for anyone who can rotate source addresses."""
    _buckets.clear()
    now = time.monotonic()
    for i in range(500):
        _buckets.allow(f"ip:10.0.0.{i}", limit=100, now=now, max_keys=50)
    assert len(_buckets.hits) <= 50


def test_eviction_is_least_recently_used():
    _buckets.clear()
    now = time.monotonic()
    _buckets.allow("keep", 100, now, max_keys=3)
    _buckets.allow("drop", 100, now, max_keys=3)
    _buckets.allow("keep", 100, now, max_keys=3)      # touch, so it is not oldest
    _buckets.allow("c", 100, now, max_keys=3)
    _buckets.allow("d", 100, now, max_keys=3)
    assert "keep" in _buckets.hits
    assert "drop" not in _buckets.hits


# ===================================================================== #
# Spend control
# ===================================================================== #
def test_budget_is_not_enforced_when_unset(settings_env):
    settings_env(AI_DAILY_BUDGET_USD="0")
    budget.record(1000.0)
    assert budget.exhausted() is False


def test_budget_blocks_once_exhausted(settings_env):
    settings_env(AI_DAILY_BUDGET_USD="1.0")
    budget.reset()
    assert budget.exhausted() is False
    budget.record(0.99)
    assert budget.exhausted() is False
    budget.record(0.02)
    assert budget.exhausted() is True


def test_exhausted_budget_refuses_ai_routes_but_not_documentation(as_doctor, settings_env):
    """Losing AI assistance must not stop a physician documenting a patient."""
    settings_env(AI_DAILY_BUDGET_USD="0.01", GEMINI_API_KEY="k")
    budget.reset()
    budget.record(1.0)

    blocked = as_doctor.post("/scribe/live", json={"transcript": "chest pain since morning"})
    assert blocked.status_code == 429
    assert "budget" in blocked.json()["detail"].lower()

    # The local risk prompt costs nothing and must keep working.
    still_works = as_doctor.post("/scribe/risk", json={
        "age": 55, "complaints": [{"text": "chest pain"}], "vitals": {"spo2": "90"}})
    assert still_works.status_code == 200


def test_budget_status_reports_remaining(settings_env):
    settings_env(AI_DAILY_BUDGET_USD="2.0")
    budget.reset()
    budget.record(0.5)
    status = budget.status()
    assert status["spent_usd"] == 0.5
    assert status["remaining_usd"] == 1.5
    assert status["enforced"] is True


# ===================================================================== #
# Metrics
# ===================================================================== #
def test_counters_gauges_and_histograms_render_in_prometheus_format():
    r = Registry()
    r.describe("cma_test_total", "A test counter.")
    r.inc("cma_test_total", {"outcome": "ok"})
    r.inc("cma_test_total", {"outcome": "ok"})
    r.gauge("cma_test_gauge", 42.0)
    r.observe("cma_test_seconds", 0.3)

    text = r.render()
    assert "# HELP cma_test_total A test counter." in text
    assert '# TYPE cma_test_total counter' in text
    assert 'cma_test_total{outcome="ok"} 2' in text
    assert "cma_test_gauge 42" in text
    assert 'cma_test_seconds_bucket{le="0.5"} 1' in text
    assert 'cma_test_seconds_bucket{le="+Inf"} 1' in text
    assert "cma_test_seconds_count 1" in text


def test_histogram_buckets_are_cumulative():
    r = Registry()
    for seconds in (0.01, 0.2, 3.0):
        r.observe("h_seconds", seconds)
    lines = {line.split(" ")[0]: line.split(" ")[1] for line in r.render().splitlines()
             if line.startswith("h_seconds_bucket")}
    assert lines['h_seconds_bucket{le="0.05"}'] == "1"
    assert lines['h_seconds_bucket{le="0.25"}'] == "2"
    assert lines['h_seconds_bucket{le="5.0"}'] == "3"


def test_label_values_are_escaped():
    r = Registry()
    r.inc("x_total", {"detail": 'has "quotes" and \\ backslash'})
    assert r"has \"quotes\" and \\ backslash" in r.render()


def test_http_requests_are_measured(as_doctor):
    registry.reset()
    as_doctor.get("/health")
    counters = registry.snapshot()["counters"]
    assert counters.get("cma_http_requests_total{method=GET,route=/health,status=2xx}") == 1
    assert registry.snapshot()["histograms"]["cma_http_request_seconds{route=/health}"]["count"] == 1


def test_route_labels_collapse_ids_to_avoid_a_series_per_patient():
    assert route_template("/patients/11111111-1111-1111-1111-111111111111/memory") == \
        "/patients/{id}/memory"
    assert route_template("/visits/42") == "/visits/{id}"
    assert route_template("/patients") == "/patients"


def test_metrics_endpoint_can_be_token_gated(client, settings_env):
    settings_env(METRICS_TOKEN="s3cret")
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")


def test_metrics_endpoint_can_be_disabled(client, settings_env):
    settings_env(METRICS_ENABLED="false")
    assert client.get("/metrics").status_code == 404


# ===================================================================== #
# PHI-aware logging
# ===================================================================== #
@pytest.mark.parametrize("key", ["q", "name", "phone", "email", "token",
                                 "uhid", "dob", "address", "pincode", "apikey"])
def test_identifying_query_parameters_are_redacted(key):
    """A log full of `q=Sharma` is a log full of patient names."""
    assert _redact({key: "Sharma"})[key] == "***"


def test_non_identifying_parameters_survive_redaction():
    assert _redact({"scope": "mine", "limit": "50"}) == {"scope": "mine", "limit": "50"}


def test_request_id_is_returned_and_echoed(as_doctor):
    generated = as_doctor.get("/health")
    assert generated.headers["x-request-id"]
    echoed = as_doctor.get("/health", headers={"x-request-id": "trace-abc"})
    assert echoed.headers["x-request-id"] == "trace-abc"


# ===================================================================== #
# Configuration
# ===================================================================== #
def test_missing_required_settings_fail_fast(monkeypatch):
    """A misconfigured server must refuse to start, not fail on the first
    patient request."""
    from pydantic import ValidationError

    from app.core.config import Settings

    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    missing = {e["loc"][0] for e in exc.value.errors()}
    assert missing == {"supabase_url", "supabase_anon_key", "supabase_service_role_key"}


def test_production_warnings_flag_dangerous_configuration(settings_env):
    s = settings_env(ENVIRONMENT="production", RATE_LIMIT_ENABLED="false",
                     FRONTEND_ORIGIN="http://app.example.com", METRICS_TOKEN="", SENTRY_DSN="")
    warnings = " ".join(s.production_warnings())
    assert "rate limiting is disabled" in warnings
    assert "plaintext http://" in warnings
    assert "METRICS_TOKEN" in warnings


def test_development_raises_no_production_warnings(settings_env):
    s = settings_env(ENVIRONMENT="development", RATE_LIMIT_ENABLED="false")
    assert s.production_warnings() == []


def test_multiple_frontend_origins_are_supported(settings_env):
    s = settings_env(FRONTEND_ORIGIN="https://a.example.com, https://b.example.com")
    assert s.allowed_origins() == ["https://a.example.com", "https://b.example.com"]


def test_configured_providers_never_leak_a_key(settings_env):
    s = settings_env(GEMINI_API_KEY="super-secret", OPENAI_API_KEY="also-secret",
                     SYNTHESIS_API_BASE="https://internal.example.com")
    snapshot = str(s.configured_providers())
    assert "super-secret" not in snapshot
    assert "also-secret" not in snapshot
    assert "internal.example.com" not in snapshot
    assert s.configured_providers()["llm_chain"] == ["gemini", "openai"]


def test_invalid_environment_is_rejected(monkeypatch):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(supabase_url="x", supabase_anon_key="x", supabase_service_role_key="x",
                 environment="prod")  # type: ignore[call-arg]


# ===================================================================== #
# Error handling
# ===================================================================== #
def test_unhandled_errors_do_not_leak_internals(as_doctor, monkeypatch):
    """A stack trace or a connection string in an API response is both an
    information disclosure and useless to the clinician reading it."""

    async def boom(*args, **kwargs):
        raise RuntimeError("postgres://user:password@db.internal/prod")

    # Patch the name the router actually resolved at import time.
    monkeypatch.setattr("app.api.routers.health.ping", boom)

    r = as_doctor.get("/health/ready")
    assert r.status_code == 500
    body = r.json()
    assert "password" not in str(body)
    assert "postgres://" not in str(body)
    assert "RuntimeError" not in str(body)
    # The correlation id ties the sanitised response to the full server log.
    assert body["request_id"]
    assert r.headers["x-request-id"] == body["request_id"]


# ===================================================================== #
# Reproducibility
# ===================================================================== #
def test_env_example_documents_every_setting():
    """A setting that exists in code but not in .env.example is a setting the
    next developer discovers by reading the source or by hitting a bug."""
    import re
    from pathlib import Path

    from app.core.config import Settings

    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    documented = {m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]*)=", example, re.M)}
    declared = {name.upper() for name in Settings.model_fields}

    undocumented = declared - documented
    assert not undocumented, f"settings missing from .env.example: {sorted(undocumented)}"

    # And nothing documented that no longer exists — a stale variable is worse
    # than a missing one, because setting it appears to work.
    test_only = {"CMA_TEST_DATABASE_URL"}
    stale = documented - declared - test_only
    assert not stale, f".env.example documents settings that no longer exist: {sorted(stale)}"


def test_env_example_ships_no_credential_values():
    """The template must be safe to commit and safe to copy.

    `RATE_LIMIT_MAX_KEYS` is a tuning number that happens to contain "KEY", so
    the check is by exact setting name rather than by substring — a substring
    rule that produces false positives is a rule people delete.
    """
    from pathlib import Path

    from app.core.config import Settings

    secret_settings = {
        name.upper() for name in Settings.model_fields
        if any(name.endswith(suffix) for suffix in
               ("_api_key", "_anon_key", "_service_role_key", "_secret", "_token", "_dsn"))
    }
    assert secret_settings, "expected to find secret-shaped settings"

    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    for line in example.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip() in secret_settings:
            assert not value.split("#")[0].strip(), f"{key} ships a value in .env.example"


def test_no_machine_specific_paths_in_the_source():
    """A hard-coded home directory makes the repository unrunnable elsewhere."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"/(Users|home)/[a-z][a-z0-9_.-]+/", re.IGNORECASE)
    offenders = []
    for path in [*root.glob("app/**/*.py"), *root.glob("scripts/**/*.py"), *root.glob("tests/**/*.py")]:
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}:{i}")
    assert not offenders, f"machine-specific paths: {offenders}"
