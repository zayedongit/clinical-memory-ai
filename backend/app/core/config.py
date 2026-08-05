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

    # Clinical Synthesis Clinical Synthesis API (external decision-support brain).
    # Base URL is treated as a secret (unauthenticated service on a shared budget);
    # set it in backend env, never commit. API key is optional until the gate exists.
    synthesis_api_base: str = ""            # e.g. https://synthesis-api.internal
    synthesis_api_key: str = ""             # sent as X-API-Key when present

    # App
    frontend_origin: str = "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
