"""Structured logging, request correlation, metrics middleware, and Sentry.

Request and response bodies are never logged. Transcripts, notes, entities and
prescriptions are all PHI, and a log aggregator is not a clinical record
system. What is logged: method, templated route, status, duration, a
correlation id, and query parameters with anything identifying redacted.

The redaction list is deliberately broad. `q` is redacted because it is a
patient search — a log full of `q=Sharma` is a log full of patient names.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import get_settings
from .metrics import registry

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_REDACT = ("phone", "name", "email", "token", "apikey", "authorization", "q",
           "uhid", "dob", "address", "pincode")

# UUIDs and other ids in a path would give every patient their own metric
# series. Collapse them so the route label stays low-cardinality.
_UUID = re.compile(r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NUMID = re.compile(r"/\d+")


def route_template(path: str) -> str:
    return _NUMID.sub("/{id}", _UUID.sub("/{id}", path)) or "/"


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
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def init_sentry() -> bool:
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
    """Correlation id, PHI-safe access log, and request metrics."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        tok = request_id_var.set(rid)
        start = time.perf_counter()
        log = logging.getLogger("request")
        route = route_template(request.url.path)
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:
            duration = time.perf_counter() - start
            registry.inc("cma_http_requests_total", {
                "route": route, "method": request.method,
                "status": f"{status_code // 100}xx",
            })
            registry.observe("cma_http_request_seconds", duration, {"route": route})
            log.info("http", extra={"extra_fields": {
                "method": request.method,
                "route": route,
                "status": status_code,
                "dur_ms": round(duration * 1000, 1),
                "query": _redact(dict(request.query_params)),
            }})
            request_id_var.reset(tok)
