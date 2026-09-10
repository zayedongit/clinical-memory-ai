"""Liveness, readiness, and metrics.

Three separate things that are often conflated:

* ``/health``  — is this process running? Never touches a dependency, so an
  orchestrator does not kill a healthy server because the database blinked.
* ``/health/ready`` — can it actually serve? Checks the database and reports
  which AI capabilities are configured. This is what a load balancer should
  gate traffic on.
* ``/metrics`` — Prometheus exposition of the counters and histograms.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Response, status

from ...ai import providers, stt
from ...core.budget import budget
from ...core.config import get_settings
from ...core.metrics import registry
from ...core.supabase import ping
from ...ml import risk_model

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(response: Response) -> dict:
    s = get_settings()
    db_ok, db_detail = await ping()

    checks = {
        "database": {"ok": db_ok, "detail": db_detail},
        "stt": {"ok": bool(stt.providers()), "chain": stt.providers()},
        "llm": {"ok": bool(providers.candidates()), **providers.describe()},
        "decision_support": {"ok": bool(s.synthesis_api_base), "configured": bool(s.synthesis_api_base)},
        "risk_model": {"ok": bool(risk_model.metrics_summary()),
                       "performance": risk_model.metrics_summary()},
    }
    # Only the database is required to serve. A missing AI provider degrades
    # the product; it does not make the server unfit to receive traffic, and
    # marking it unready would take documentation offline to protect a
    # convenience feature.
    ready_now = db_ok
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "ready": ready_now,
        "environment": s.environment,
        "checks": checks,
        "budget": budget.status(),
        "configuration_warnings": s.production_warnings(),
    }


@router.get("/metrics")
def metrics(authorization: str | None = Header(default=None)) -> Response:
    s = get_settings()
    if not s.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Metrics are disabled")
    # A metrics endpoint carries operational detail (error rates, spend, call
    # volumes). It is optionally token-gated so it can be exposed on a public
    # ingress without handing that detail to anyone who asks.
    if s.metrics_token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if supplied != s.metrics_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Metrics token required")
    return Response(registry.render(), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/metrics/summary")
def metrics_summary(authorization: str | None = Header(default=None)) -> dict:
    """The same series as JSON, for the engineering dashboard in the frontend."""
    s = get_settings()
    if not s.metrics_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Metrics are disabled")
    if s.metrics_token:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if supplied != s.metrics_token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Metrics token required")
    return {
        **registry.snapshot(),
        "budget": budget.status(),
        "risk_model": risk_model.metrics_summary(),
    }
