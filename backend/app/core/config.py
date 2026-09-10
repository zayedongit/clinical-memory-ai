"""Application settings, loaded from environment (.env in dev).

Constructing `Settings` is the boot-time contract check: the Supabase URL and
keys have no default, so a misconfigured server refuses to start rather than
failing on the first patient request.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Supabase (required) -----------------------------------------
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # --- AI providers -------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    # Tried in order after `gemini_model`, before falling through to OpenAI.
    gemini_fallbacks: str = "gemini-2.0-flash,gemini-2.5-flash-lite"

    openai_api_key: str = ""
    openai_llm_model: str = "gpt-4o-mini"
    openai_stt_model: str = "gpt-4o-transcribe"

    sarvam_api_key: str = ""
    sarvam_stt_model: str = "saarika:v2.5"
    # "unknown" asks Sarvam to auto-detect, which is what Hindi-English
    # code-mixed speech needs; pinning a language mangles the other one.
    sarvam_stt_language: str = "unknown"

    ai_temperature: float = 0.2
    ai_prices_json: str = ""          # {"model": [usd_per_1m_in, usd_per_1m_out]}

    # --- Clinical Synthesis API (external decision-support service) ----
    # Base URL is a shared secret (the upstream is unauthenticated on a shared
    # budget). Called only server-side, never from the browser.
    synthesis_api_base: str = ""
    synthesis_api_key: str = ""
    synthesis_timeout_s: float = 30.0

    # --- Limits --------------------------------------------------------
    # An audio segment the browser records between two flushes. 25 MB is the
    # provider ceiling; the live client sends ~12 s slices, so anything near
    # this is either a very long single-shot recording or an abusive upload.
    max_audio_bytes: int = 25 * 1024 * 1024
    max_transcript_chars: int = 60_000
    stt_timeout_s: float = 180.0

    # --- App -----------------------------------------------------------
    # Comma-separated list; the first entry is the canonical frontend origin.
    frontend_origin: str = "http://localhost:3000"

    # --- Observability --------------------------------------------------
    environment: str = "development"        # development | staging | production
    log_level: str = "INFO"
    sentry_dsn: str = ""
    metrics_enabled: bool = True
    # Bearer token required to read /metrics. Empty means the endpoint is
    # open, which is only acceptable when it is not internet-reachable.
    metrics_token: str = ""

    # --- Rate limiting (sliding window, per caller) ---------------------
    rate_limit_enabled: bool = True
    rate_limit_default_per_min: int = 120
    rate_limit_ai_per_min: int = 90          # STT / LLM / decision-support routes
    # Hard ceiling on tracked callers, so the limiter cannot be turned into a
    # memory-exhaustion vector by rotating source addresses.
    rate_limit_max_keys: int = 20_000

    # --- Spend controls --------------------------------------------------
    # Estimated USD an instance may spend on AI per day before the expensive
    # routes start refusing. Zero disables the cap.
    ai_daily_budget_usd: float = Field(default=0.0, ge=0.0)

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, v: str) -> str:
        if v not in ("development", "staging", "production"):
            raise ValueError("ENVIRONMENT must be development, staging or production")
        return v

    # --- Derived helpers -------------------------------------------------
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origin.split(",") if o.strip()]

    def gemini_fallback_models(self) -> list[str]:
        return [m.strip() for m in self.gemini_fallbacks.split(",") if m.strip()]

    def ai_price_overrides(self) -> dict[str, tuple[float, float]]:
        if not self.ai_prices_json.strip():
            return {}
        try:
            raw = json.loads(self.ai_prices_json)
            return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
        except (ValueError, TypeError, IndexError, KeyError):
            log.warning("AI_PRICES_JSON is not valid; using built-in price table")
            return {}

    def configured_providers(self) -> dict:
        """A non-secret snapshot of which capabilities are wired, for startup
        logs and /health. Never includes a key or a URL."""
        stt = [p for p, on in (("openai", self.openai_api_key), ("sarvam", self.sarvam_api_key)) if on]
        llm = [p for p, on in (("gemini", self.gemini_api_key), ("openai", self.openai_api_key)) if on]
        return {
            "environment": self.environment,
            "stt_chain": stt or ["none"],
            "llm_chain": llm or ["none"],
            "decision_support": bool(self.synthesis_api_base),
            "sentry": bool(self.sentry_dsn),
            "rate_limit": self.rate_limit_enabled,
            "metrics": self.metrics_enabled,
            "ai_daily_budget_usd": self.ai_daily_budget_usd,
        }

    def production_warnings(self) -> list[str]:
        """Configuration that is fine in development and dangerous in production."""
        if self.environment != "production":
            return []
        problems = []
        if not self.rate_limit_enabled:
            problems.append("rate limiting is disabled")
        if any(o.startswith("http://") for o in self.allowed_origins()):
            problems.append("FRONTEND_ORIGIN contains a plaintext http:// origin")
        if "*" in self.allowed_origins():
            problems.append("FRONTEND_ORIGIN is a wildcard")
        if self.metrics_enabled and not self.metrics_token:
            problems.append("/metrics is exposed without METRICS_TOKEN")
        if not self.sentry_dsn:
            problems.append("SENTRY_DSN is unset; unhandled errors will only reach stdout")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
