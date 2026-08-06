"""FastAPI application entrypoint."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routers import (clinics, conditions, drugs, health, match, me, patients,
                          scribe, synthesis, visits)
from .core.config import get_settings
from .core.observability import RequestLogMiddleware, init_sentry, setup_logging
from .core.ratelimit import RateLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log = logging.getLogger("startup")
    # Fail-fast: constructing Settings validates that required secrets (Supabase
    # URL + keys) are present. A misconfigured server should refuse to start, not
    # fail on the first patient request.
    try:
        settings = get_settings()
    except Exception as e:  # noqa: BLE001 — surface config errors clearly at boot
        logging.getLogger("startup").error("Invalid configuration: %s", e)
        raise
    init_sentry()
    log.info("startup", extra={"extra_fields": settings.configured_providers()})
    yield


app = FastAPI(title="Clinical Memory AI API", version="0.1.0", lifespan=lifespan)

# Middleware are applied outermost-last. Order gives us:
#   CORS -> RequestLog -> RateLimit -> app
# so CORS headers wrap every response (incl. 429s) and the request log captures
# rate-limited requests too.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)

_frontend_origin = os.getenv("FRONTEND_ORIGIN", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id", "Retry-After"],
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
