"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routers import (
    clinics,
    conditions,
    drugs,
    health,
    match,
    me,
    patients,
    scribe,
    synthesis,
    visits,
)
from .core.config import get_settings
from .core.observability import (
    RequestLogMiddleware,
    init_sentry,
    request_id_var,
    route_template,
    setup_logging,
)
from .core.ratelimit import RateLimitMiddleware
from .core.supabase import close_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log = logging.getLogger("startup")
    # Fail fast: constructing Settings validates that the required secrets are
    # present. A misconfigured server should refuse to start, not fail on the
    # first patient request.
    try:
        settings = get_settings()
    except Exception as e:
        log.error("Invalid configuration: %s", e)
        raise
    init_sentry()
    log.info("startup", extra={"extra_fields": settings.configured_providers()})
    for warning in settings.production_warnings():
        log.warning("production_config_warning", extra={"extra_fields": {"issue": warning}})
    try:
        yield
    finally:
        await close_client()


app = FastAPI(
    title="Clinical Memory AI API",
    version="1.0.0",
    description=(
        "Clinical documentation and longitudinal patient memory. Every AI output is a "
        "draft for physician review; the backend refuses to finalise a note without an "
        "explicit attestation from a user whose role may sign."
    ),
    lifespan=lifespan,
)

# Middleware apply outermost-last, giving CORS -> RequestLog -> RateLimit -> app,
# so CORS headers wrap every response (including 429s) and the access log
# records rate-limited requests too.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)

def _origins() -> list[str]:
    try:
        return get_settings().allowed_origins()
    except Exception:
        # Settings validation failures surface at startup via lifespan; CORS
        # must still be constructible so that error is the one the operator sees.
        return ["http://localhost:3000"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(),
    # No cookies are used — authentication is a bearer token — so credentialed
    # CORS buys nothing and would force the origin list to be exact anyway.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    expose_headers=["x-request-id", "Retry-After"],
    max_age=600,
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Never leak an internal error to a clinical client.

    A stack trace or a database message in an API response is both an
    information disclosure and useless to the physician reading it. The
    correlation id is returned instead, so a support request can be tied to the
    exact log line without the client ever seeing the detail.
    """
    rid = request_id_var.get()
    logging.getLogger("unhandled").exception(
        "unhandled_error",
        extra={"extra_fields": {"route": route_template(request.url.path), "request_id": rid}},
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong on our side. Nothing was saved.",
                 "request_id": rid},
        headers={"x-request-id": rid},
    )


app.include_router(health.router)
app.include_router(me.router)
app.include_router(clinics.router)
app.include_router(patients.router)
app.include_router(match.router)
app.include_router(conditions.router)
app.include_router(scribe.router)
app.include_router(visits.router)
app.include_router(drugs.router)
app.include_router(synthesis.router)
