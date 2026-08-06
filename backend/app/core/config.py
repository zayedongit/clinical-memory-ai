"""Application settings, loaded from environment (.env in dev)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str
    supabase_jwt_secret: str = ""

    # AI providers (Phase 1)
    gemini_api_key: str = ""
    sarvam_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    sarvam_stt_model: str = "saarika:v2.5"
    sarvam_stt_language: str = "unknown"  # auto-detect / code-mixed (Hinglish)

    # Speech-to-text: OpenAI gpt-4o-transcribe (preferred — 99+ langs, up to 25 min /
    # 25 MB per call, strong on Hinglish). Falls back to Sarvam if this key is unset.
    openai_api_key: str = ""
    openai_stt_model: str = "gpt-4o-transcribe"

    # Clinical Synthesis API (external decision-support brain).
    # Base URL is treated as a secret (unauthenticated service on a shared budget);
    # set it in backend env, never commit. API key is optional until the gate exists.
    synthesis_api_base: str = ""            # Clinical Synthesis API base URL (set in .env)
    synthesis_api_key: str = ""             # sent as X-API-Key when present

    # App
    frontend_origin: str = "http://localhost:3000"

    # Observability
    environment: str = "development"        # development | staging | production
    log_level: str = "INFO"
    sentry_dsn: str = ""                     # optional; error tracking off when blank

    # Rate limiting (per client IP, 60s sliding window)
    rate_limit_enabled: bool = True
    rate_limit_default_per_min: int = 120
    rate_limit_ai_per_min: int = 30          # STT / LLM / decision-support routes

    def configured_providers(self) -> dict:
        """A non-secret snapshot of which capabilities are wired — for startup logs."""
        return {
            "environment": self.environment,
            "stt": "openai" if self.openai_api_key else ("sarvam" if self.sarvam_api_key else "none"),
            "llm": "gemini" if self.gemini_api_key else "none",
            "decision_support": bool(self.synthesis_api_base),
            "sentry": bool(self.sentry_dsn),
            "rate_limit": self.rate_limit_enabled,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
