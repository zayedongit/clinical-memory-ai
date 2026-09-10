"""Speech-to-text with runtime failover across providers.

The previous implementation picked a provider at *configuration* time: if
`OPENAI_API_KEY` was set it used OpenAI and, on failure, returned 502 —
Sarvam was never tried even when it was configured and healthy. In a live
consultation that means the doctor's speech is simply lost.

This module treats the configured providers as a chain, exactly like the LLM
side, and fails over at runtime.

Language handling: the target user speaks Hindi–English code-mixed
("Hinglish"). Both providers are asked to auto-detect rather than being
pinned to a language, because pinning to `hi` mangles the English clinical
vocabulary (drug names, "chest pain") and pinning to `en` drops the Hindi.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from ..core.config import get_settings
from ..core.metrics import Timer, registry

log = logging.getLogger("ai.stt")

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"

_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}

# Estimated USD per minute of audio, same caveat as the LLM price table.
_PRICE_PER_MIN = {"openai": 0.006, "sarvam": 0.003}


@dataclass
class Transcript:
    text: str
    provider: str
    language: str | None = None
    latency_s: float = 0.0
    cost_usd: float = 0.0
    attempts: list[str] = field(default_factory=list)

    @property
    def fell_back(self) -> bool:
        return len(self.attempts) > 1


class AllSTTFailed(RuntimeError):
    def __init__(self, attempts: list[str]) -> None:
        self.attempts = attempts
        super().__init__(f"speech-to-text: all providers failed ({'; '.join(attempts) or 'none configured'})")


async def _openai(client: httpx.AsyncClient, filename: str, audio: bytes, ctype: str) -> tuple[str, str | None]:
    s = get_settings()
    r = await client.post(
        OPENAI_STT_URL,
        headers={"Authorization": f"Bearer {s.openai_api_key}"},
        data={"model": s.openai_stt_model, "response_format": "json"},
        files={"file": (filename, audio, ctype)},
    )
    r.raise_for_status()
    body = r.json()
    return body.get("text", ""), body.get("language")


async def _sarvam(client: httpx.AsyncClient, filename: str, audio: bytes, ctype: str) -> tuple[str, str | None]:
    s = get_settings()
    r = await client.post(
        SARVAM_STT_URL,
        headers={"api-subscription-key": s.sarvam_api_key},
        data={"model": s.sarvam_stt_model, "language_code": s.sarvam_stt_language},
        files={"file": (filename, audio, ctype)},
    )
    r.raise_for_status()
    body = r.json()
    return body.get("transcript", ""), body.get("language_code")


_ADAPTERS = {"openai": _openai, "sarvam": _sarvam}


def providers() -> list[str]:
    s = get_settings()
    chain = []
    if s.openai_api_key:
        chain.append("openai")
    if s.sarvam_api_key:
        chain.append("sarvam")
    return chain


async def transcribe(audio: bytes, *, filename: str, content_type: str) -> Transcript:
    chain = providers()
    if not chain:
        raise AllSTTFailed([])

    attempts: list[str] = []
    async with httpx.AsyncClient(timeout=get_settings().stt_timeout_s) as client:
        for index, provider in enumerate(chain):
            labels = {"capability": "stt", "provider": provider}
            try:
                with Timer("cma_ai_call_seconds", labels) as t:
                    text, language = await _ADAPTERS[provider](client, filename, audio, content_type)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                attempts.append(f"{provider}: HTTP {status}")
                registry.inc("cma_ai_calls_total", {**labels, "outcome": f"http_{status}"})
                if status not in _TRANSIENT and index < len(chain) - 1:
                    log.warning("stt_provider_rejected",
                                extra={"extra_fields": {"provider": provider, "status": status}})
                continue
            except (TimeoutError, httpx.HTTPError) as e:
                attempts.append(f"{provider}: {type(e).__name__}")
                registry.inc("cma_ai_calls_total", {**labels, "outcome": "transport_error"})
                continue

            attempts.append(f"{provider}: ok")
            cost = estimate_cost(provider, len(audio))
            registry.inc("cma_ai_calls_total", {**labels, "outcome": "ok"})
            registry.inc("cma_ai_cost_usd_total", labels, by=cost)
            if len(attempts) > 1:
                registry.inc("cma_ai_fallbacks_total", labels)
                log.warning("stt_fallback", extra={"extra_fields": {"attempts": attempts}})
            return Transcript(text=text or "", provider=provider, language=language,
                              latency_s=round(t.elapsed, 3), cost_usd=cost, attempts=attempts)

    log.error("stt_all_failed", extra={"extra_fields": {"attempts": attempts}})
    raise AllSTTFailed(attempts)


def estimate_cost(provider: str, byte_len: int) -> float:
    """Rough spend estimate from payload size.

    The browser sends 16-bit mono PCM WAV, so bytes/(2*sample_rate) is the
    duration. We assume 48 kHz (what `AudioContext` reports on every desktop
    browser we have tested); a wrong assumption skews the cost series, not
    the transcript.
    """
    seconds = max(byte_len - 44, 0) / (2 * 48000)
    return (seconds / 60.0) * _PRICE_PER_MIN.get(provider, 0.0)
