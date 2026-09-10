"""LLM providers behind one interface, with real cross-provider failover.

What "failover" means here, precisely, because the README used to overstate it:

* A *capability* (structuring a note, extracting an encounter) is served by an
  ordered list of `(provider, model)` candidates.
* Candidates are tried in order. A candidate is retried on the next one when
  the failure is transient (429, 5xx, timeout, connection error) or when the
  response cannot be parsed into JSON. Non-transient failures (401, 400) skip
  the rest of *that provider's* models — a bad key will not get better — but
  still fall through to the next provider, which is the whole point.
* Falling through is counted, so "how often does Gemini fail" is a number and
  not a feeling.

Everything is measured: latency, outcome, tokens and estimated cost per
provider per capability.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.config import get_settings
from ..core.metrics import Timer, registry
from . import json_repair

log = logging.getLogger("ai.providers")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Transient HTTP statuses: worth trying the next candidate.
_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}

# Published list prices in USD per 1M tokens, used only to estimate spend.
# These are a snapshot taken when this file was written and WILL drift; the
# number they produce is a planning estimate, not a bill. Override via
# AI_PRICES_JSON if you need current figures.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    # model: (usd per 1M input tokens, usd per 1M output tokens)
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-flash-lite-latest": (0.10, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}


@dataclass
class LLMResult:
    """One successful structured generation, plus how it was obtained."""

    data: dict
    provider: str
    model: str
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    repaired: bool = False
    attempts: list[str] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return len(self.attempts) > 1


class AllProvidersFailed(RuntimeError):
    """Every candidate for a capability failed. Carries what each one said."""

    def __init__(self, capability: str, attempts: list[str]) -> None:
        self.capability = capability
        self.attempts = attempts
        super().__init__(f"{capability}: all providers failed ({'; '.join(attempts) or 'none configured'})")


def _price(model: str) -> tuple[float, float]:
    s = get_settings()
    table = {**DEFAULT_PRICES, **s.ai_price_overrides()}
    if model in table:
        return table[model]
    # Unknown model: fall back to the closest configured family so the cost
    # series stays populated rather than silently reading zero.
    for known, price in table.items():
        if model.startswith(known.rsplit("-", 1)[0]):
            return price
    return (0.0, 0.0)


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = _price(model)
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


# --------------------------------------------------------------------- #
# Per-provider adapters. Each returns (text, prompt_tokens, completion_tokens)
# or raises httpx.HTTPStatusError / httpx.HTTPError.
# --------------------------------------------------------------------- #
async def _call_gemini(client: httpx.AsyncClient, model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    s = get_settings()
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": s.ai_temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    r = await client.post(
        f"{GEMINI_BASE}/models/{model}:generateContent",
        params={"key": s.gemini_api_key},
        json=payload,
    )
    r.raise_for_status()
    body = r.json()
    cand = (body.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    usage = body.get("usageMetadata") or {}
    return text, int(usage.get("promptTokenCount") or 0), int(usage.get("candidatesTokenCount") or 0)


async def _call_openai(client: httpx.AsyncClient, model: str, prompt: str, max_tokens: int) -> tuple[str, int, int]:
    s = get_settings()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": s.ai_temperature,
        "max_tokens": max_tokens,
    }
    r = await client.post(
        OPENAI_CHAT_URL,
        headers={"Authorization": f"Bearer {s.openai_api_key}"},
        json=payload,
    )
    r.raise_for_status()
    body = r.json()
    text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


_ADAPTERS = {"gemini": _call_gemini, "openai": _call_openai}


def candidates() -> list[tuple[str, str]]:
    """The ordered `(provider, model)` list, from configuration.

    Gemini first (cheapest for this workload), then OpenAI as a genuinely
    different vendor — a fallback that shares an outage with its primary is
    not a fallback. Models within a provider are tried before moving on,
    because provider-side model overload is the most common failure.
    """
    s = get_settings()
    out: list[tuple[str, str]] = []
    if s.gemini_api_key:
        for m in [s.gemini_model, *s.gemini_fallback_models()]:
            if m and ("gemini", m) not in out:
                out.append(("gemini", m))
    if s.openai_api_key:
        for m in [s.openai_llm_model]:
            if m and ("openai", m) not in out:
                out.append(("openai", m))
    return out


async def generate_json(
    prompt: str,
    *,
    capability: str,
    max_tokens: int = 4096,
    timeout: float = 60.0,
) -> LLMResult:
    """Run `prompt` through the candidate chain until one returns valid JSON."""
    chain = candidates()
    if not chain:
        raise AllProvidersFailed(capability, [])

    attempts: list[str] = []
    skip_provider: set[str] = set()

    async with httpx.AsyncClient(timeout=timeout) as client:
        for index, (provider, model) in enumerate(chain):
            if provider in skip_provider:
                continue
            labels = {"capability": capability, "provider": provider}
            try:
                with Timer("cma_ai_call_seconds", labels) as t:
                    text, ptok, ctok = await _ADAPTERS[provider](client, model, prompt, max_tokens)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                attempts.append(f"{provider}/{model}: HTTP {status}")
                registry.inc("cma_ai_calls_total", {**labels, "outcome": f"http_{status}"})
                if status not in _TRANSIENT:
                    # A bad key or malformed request will fail identically on
                    # every model from this provider. Move to the next vendor.
                    skip_provider.add(provider)
                await _backoff(index, chain)
                continue
            except (TimeoutError, httpx.HTTPError) as e:
                attempts.append(f"{provider}/{model}: {type(e).__name__}")
                registry.inc("cma_ai_calls_total", {**labels, "outcome": "transport_error"})
                await _backoff(index, chain)
                continue

            parsed = json_repair.parse(text)
            if parsed.repaired:
                registry.inc("cma_ai_json_repairs_total", labels)
            if not parsed:
                attempts.append(f"{provider}/{model}: unparseable ({parsed.reason})")
                registry.inc("cma_ai_calls_total", {**labels, "outcome": "bad_json"})
                await _backoff(index, chain)
                continue

            cost = _cost(model, ptok, ctok)
            registry.inc("cma_ai_calls_total", {**labels, "outcome": "ok"})
            registry.inc("cma_ai_tokens_total", labels, by=ptok + ctok)
            registry.inc("cma_ai_cost_usd_total", labels, by=cost)
            attempts.append(f"{provider}/{model}: ok")
            if len(attempts) > 1:
                registry.inc("cma_ai_fallbacks_total", labels)
                log.warning(
                    "ai_fallback",
                    extra={"extra_fields": {"capability": capability, "attempts": attempts}},
                )
            return LLMResult(
                data=parsed.data or {}, provider=provider, model=model,
                latency_s=round(t.elapsed, 3), prompt_tokens=ptok, completion_tokens=ctok,
                cost_usd=cost, repaired=parsed.repaired, attempts=attempts,
            )

    log.error("ai_all_failed", extra={"extra_fields": {"capability": capability, "attempts": attempts}})
    raise AllProvidersFailed(capability, attempts)


async def _backoff(index: int, chain: list) -> None:
    """A short pause before the next candidate — provider overload clears fast.

    Deliberately brief: this sits in the request path of a live consultation,
    so a long retry ladder is worse than failing over quickly.
    """
    if index < len(chain) - 1:
        await asyncio.sleep(min(0.5 * (index + 1), 2.0))


def describe() -> dict[str, Any]:
    """Non-secret view of the configured chain, for /health and the dashboard."""
    return {
        "chain": [f"{p}/{m}" for p, m in candidates()],
        "configured": bool(candidates()),
    }
