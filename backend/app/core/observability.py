"""Structured logging, optional Sentry, and request-logging middleware.

We deliberately NEVER log request or response bodies — transcripts, notes, and
patient data are PHI. Only method, path, status, timing, a per-request id, and
redacted query params are logged. Sentry, if configured, runs with PII off.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import get_settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Query-param keys that could carry identifiers we don't want in logs verbatim.
_REDACT = ("phone", "name", "email", "token", "apikey", "authorization", "q")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(getattr(record, "extra_fields", {}) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    s = get_settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(s.log_level.upper())
    # Quiet chatty client libraries — we do our own request logging.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init_sentry() -> bool:
    """Initialise Sentry only if a DSN is set and the SDK is installed."""
    s = get_settings()
    if not s.sentry_dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=s.sentry_dsn, environment=s.environment,
            send_default_pii=False, traces_sample_rate=0.1,
        )
        return True
    except Exception:
        logging.getLogger("startup").warning(
            "SENTRY_DSN is set but sentry-sdk is not installed; error tracking disabled")
        return False


def _redact(params: dict) -> dict:
    return {k: ("***" if any(r in k.lower() for r in _REDACT) else v) for k, v in params.items()}


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        tok = request_id_var.set(rid)
        start = time.perf_counter()
        log = logging.getLogger("request")
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:
            log.info("http", extra={"extra_fields": {
                "method": request.method,
                "path": request.url.path,
                "status": status_code,
                "dur_ms": round((time.perf_counter() - start) * 1000, 1),
                "query": _redact(dict(request.query_params)),
            }})
            request_id_var.reset(tok)
