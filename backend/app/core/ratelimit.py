"""Dependency-free sliding-window rate limiter (per client IP + route group).

Single-instance MVP protection against runaway cost/abuse on the expensive AI
endpoints (STT, LLM, decision support). For a multi-instance deployment this
should be backed by Redis so the window is shared across workers — noted in the
reliability roadmap.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_settings

_WINDOW = 60.0                     # seconds
_AI_PREFIXES = ("/scribe", "/synthesis")


class _Buckets:
    """One sliding-window deque of hit timestamps per key."""

    def __init__(self) -> None:
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int, now: float) -> tuple[bool, float]:
        dq = self.hits[key]
        cutoff = now - _WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False, max(_WINDOW - (now - dq[0]), 1.0)
        dq.append(now)
        return True, 0.0


_buckets = _Buckets()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        if not s.rate_limit_enabled or request.method == "OPTIONS":
            return await call_next(request)

        is_ai = request.url.path.startswith(_AI_PREFIXES)
        limit = s.rate_limit_ai_per_min if is_ai else s.rate_limit_default_per_min
        key = f"{_client_ip(request)}:{'ai' if is_ai else 'default'}"

        ok, retry = _buckets.allow(key, limit, time.monotonic())
        if not ok:
            return JSONResponse(
                {"detail": "Rate limit exceeded. Please slow down and retry shortly."},
                status_code=429, headers={"Retry-After": str(int(retry))},
            )
        return await call_next(request)
