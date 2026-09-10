"""Sliding-window rate limiting, per caller and route group.

Scope, stated honestly: this is per-process. Two workers means two independent
windows and therefore twice the configured limit. It is protection against a
runaway client and accidental cost, not against a distributed attacker. Making
it correct across instances needs a shared store (Redis), which is documented
as the next step rather than pretended away.

Two things the previous version got wrong:

* It keyed on client IP only. Behind a proxy every clinic shares one address,
  so one busy consultation could rate-limit an entire building. Authenticated
  requests are now keyed by bearer-token identity, falling back to IP only for
  unauthenticated calls.

* Its bucket dictionary grew without bound — one deque per distinct IP,
  forever. Rotating source addresses turned the limiter itself into a memory
  exhaustion vector. Keys are now capped with LRU eviction.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import get_settings
from .metrics import registry

_WINDOW = 60.0

# Route prefixes that reach a paid provider. These get the AI budget.
_AI_PREFIXES = ("/scribe/transcribe", "/scribe/soap", "/scribe/live",
                "/scribe/extract", "/synthesis")

# Never rate-limited: liveness/readiness must answer even under load, or the
# orchestrator restarts a server that was merely busy.
_EXEMPT = ("/health", "/metrics", "/docs", "/openapi.json", "/redoc")


class _Buckets:
    """Bounded LRU of sliding windows."""

    def __init__(self) -> None:
        self.hits: OrderedDict[str, deque] = OrderedDict()

    def allow(self, key: str, limit: int, now: float, max_keys: int) -> tuple[bool, float]:
        dq = self.hits.get(key)
        if dq is None:
            dq = deque()
            self.hits[key] = dq
            while len(self.hits) > max_keys:
                # Evict the least recently used window. Worst case an evicted
                # caller gets a fresh allowance, which is the safe direction to
                # fail for a cost guard.
                self.hits.popitem(last=False)
        else:
            self.hits.move_to_end(key)

        cutoff = now - _WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False, max(_WINDOW - (now - dq[0]), 1.0)
        dq.append(now)
        return True, 0.0

    def clear(self) -> None:
        self.hits.clear()


_buckets = _Buckets()


def _caller_key(request: Request) -> str:
    """Identify the caller without putting anything identifying in memory.

    The bearer token is hashed, never stored: the limiter needs a stable
    identifier, not the credential itself.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and len(auth) > 16:
        return "u:" + hashlib.sha256(auth[7:].strip().encode()).hexdigest()[:16]
    fwd = request.headers.get("x-forwarded-for")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")
    return "ip:" + ip


def route_group(path: str) -> str:
    return "ai" if path.startswith(_AI_PREFIXES) else "default"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        s = get_settings()
        path = request.url.path
        if not s.rate_limit_enabled or request.method == "OPTIONS" or path.startswith(_EXEMPT):
            return await call_next(request)

        group = route_group(path)
        limit = s.rate_limit_ai_per_min if group == "ai" else s.rate_limit_default_per_min
        key = f"{_caller_key(request)}:{group}"

        ok, retry = _buckets.allow(key, limit, time.monotonic(), s.rate_limit_max_keys)
        if not ok:
            registry.inc("cma_rate_limited_total", {"group": group})
            return JSONResponse(
                {"detail": "Rate limit exceeded. Please slow down and retry shortly.",
                 "retry_after_seconds": int(retry)},
                status_code=429, headers={"Retry-After": str(int(retry))},
            )
        return await call_next(request)
