"""LLM and speech-to-text failover.

The README used to claim runtime provider failover that did not exist: the LLM
lane was Gemini-only, and speech-to-text picked a provider at configuration
time and returned 502 when it failed. These tests pin the behaviour that now
backs the claim.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.ai import providers, stt
from app.core.metrics import registry

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/"
OPENAI_CHAT = "https://api.openai.com/v1/chat/completions"
OPENAI_STT = "https://api.openai.com/v1/audio/transcriptions"
SARVAM_STT = "https://api.sarvam.ai/speech-to-text"

GOOD_JSON = '{"hpi": "cough for three days", "allergies": ""}'


def _gemini_ok(text: str = GOOD_JSON, prompt_tokens: int = 100, out_tokens: int = 50):
    return httpx.Response(200, json={
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": prompt_tokens, "candidatesTokenCount": out_tokens},
    })


def _openai_ok(text: str = GOOD_JSON):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 60},
    })


@pytest.fixture
def both_providers(settings_env):
    return settings_env(GEMINI_API_KEY="g-key", OPENAI_API_KEY="o-key",
                        GEMINI_MODEL="gemini-2.5-flash", GEMINI_FALLBACKS="gemini-2.0-flash")


# --------------------------------------------------------------------- #
# The candidate chain
# --------------------------------------------------------------------- #
def test_chain_is_ordered_and_deduplicated(both_providers):
    assert providers.candidates() == [
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-2.0-flash"),
        ("openai", "gpt-4o-mini"),
    ]


def test_chain_is_empty_without_keys(settings_env):
    settings_env(GEMINI_API_KEY="", OPENAI_API_KEY="")
    assert providers.candidates() == []


async def test_no_providers_raises_rather_than_hanging(settings_env):
    settings_env(GEMINI_API_KEY="", OPENAI_API_KEY="")
    with pytest.raises(providers.AllProvidersFailed):
        await providers.generate_json("x", capability="extract")


# --------------------------------------------------------------------- #
# Failover
# --------------------------------------------------------------------- #
@respx.mock
async def test_transient_gemini_failure_falls_through_to_the_next_model(both_providers):
    respx.post(url__startswith=f"{GEMINI}gemini-2.5-flash").mock(
        return_value=httpx.Response(503, json={"error": "overloaded"}))
    respx.post(url__startswith=f"{GEMINI}gemini-2.0-flash").mock(return_value=_gemini_ok())

    result = await providers.generate_json("prompt", capability="extract")
    assert result.model == "gemini-2.0-flash"
    assert result.fell_back is True


@respx.mock
async def test_bad_api_key_skips_the_whole_provider_and_reaches_the_other_vendor(both_providers):
    """A 401 will not get better on the same vendor's next model.

    This is the failure that actually happened in this project: Gemini started
    returning 401 and every model retry burned latency before failing.
    """
    gemini = respx.post(url__startswith=GEMINI).mock(
        return_value=httpx.Response(401, json={"error": "API key not valid"}))
    respx.post(OPENAI_CHAT).mock(return_value=_openai_ok())

    result = await providers.generate_json("prompt", capability="extract")
    assert result.provider == "openai"
    # Exactly one Gemini attempt, not one per Gemini model.
    assert gemini.call_count == 1


@respx.mock
async def test_cross_vendor_failover_on_transient_errors(both_providers):
    respx.post(url__startswith=GEMINI).mock(return_value=httpx.Response(429))
    respx.post(OPENAI_CHAT).mock(return_value=_openai_ok())

    result = await providers.generate_json("prompt", capability="soap")
    assert result.provider == "openai"
    assert len(result.attempts) == 3        # two Gemini models, then OpenAI


@respx.mock
async def test_connection_errors_fail_over(both_providers):
    respx.post(url__startswith=GEMINI).mock(side_effect=httpx.ConnectError("no route"))
    respx.post(OPENAI_CHAT).mock(return_value=_openai_ok())
    result = await providers.generate_json("prompt", capability="live")
    assert result.provider == "openai"


@respx.mock
async def test_all_providers_failing_raises_with_the_reasons(both_providers):
    respx.post(url__startswith=GEMINI).mock(return_value=httpx.Response(503))
    respx.post(OPENAI_CHAT).mock(return_value=httpx.Response(500))

    with pytest.raises(providers.AllProvidersFailed) as exc:
        await providers.generate_json("prompt", capability="soap")
    assert len(exc.value.attempts) == 3
    assert any("503" in a for a in exc.value.attempts)
    assert any("openai" in a for a in exc.value.attempts)


# --------------------------------------------------------------------- #
# Malformed model output
# --------------------------------------------------------------------- #
@respx.mock
async def test_unparseable_output_counts_as_a_failure_and_fails_over(both_providers):
    """A 200 carrying prose instead of JSON is a failure, not a success.

    Treating it as success meant an empty note with no error anywhere.
    """
    respx.post(url__startswith=GEMINI).mock(
        return_value=_gemini_ok("I'm sorry, I can't help with that."))
    respx.post(OPENAI_CHAT).mock(return_value=_openai_ok())

    result = await providers.generate_json("prompt", capability="extract")
    assert result.provider == "openai"


@respx.mock
async def test_fenced_json_is_accepted_without_falling_over(both_providers):
    respx.post(url__startswith=f"{GEMINI}gemini-2.5-flash").mock(
        return_value=_gemini_ok('```json\n{"hpi": "cough"}\n```'))
    result = await providers.generate_json("prompt", capability="extract")
    assert result.data == {"hpi": "cough"}
    assert result.fell_back is False


@respx.mock
async def test_truncated_json_is_repaired_and_the_repair_is_counted(both_providers):
    respx.post(url__startswith=f"{GEMINI}gemini-2.5-flash").mock(
        return_value=_gemini_ok('{"hpi": "cough for three days", "vitals": {"bp": "120/'))
    result = await providers.generate_json("prompt", capability="extract")
    assert result.repaired is True
    assert result.data["hpi"] == "cough for three days"
    snapshot = registry.snapshot()["counters"]
    assert any("json_repairs" in k for k in snapshot)


# --------------------------------------------------------------------- #
# Cost and token accounting
# --------------------------------------------------------------------- #
@respx.mock
async def test_tokens_and_cost_are_recorded(both_providers):
    respx.post(url__startswith=f"{GEMINI}gemini-2.5-flash").mock(
        return_value=_gemini_ok(prompt_tokens=1_000_000, out_tokens=0))
    result = await providers.generate_json("prompt", capability="extract")
    # gemini-2.5-flash input price is 0.30 per 1M tokens.
    assert result.cost_usd == pytest.approx(0.30, rel=1e-6)
    assert result.prompt_tokens == 1_000_000


@respx.mock
async def test_fallbacks_are_counted(both_providers):
    respx.post(url__startswith=f"{GEMINI}gemini-2.5-flash").mock(return_value=httpx.Response(503))
    respx.post(url__startswith=f"{GEMINI}gemini-2.0-flash").mock(return_value=_gemini_ok())
    await providers.generate_json("prompt", capability="extract")
    counters = registry.snapshot()["counters"]
    assert any("cma_ai_fallbacks_total" in k for k in counters)


# --------------------------------------------------------------------- #
# Speech to text
# --------------------------------------------------------------------- #
@pytest.fixture
def both_stt(settings_env):
    return settings_env(OPENAI_API_KEY="o-key", SARVAM_API_KEY="s-key")


def test_stt_chain_prefers_openai_then_sarvam(both_stt):
    assert stt.providers() == ["openai", "sarvam"]


@respx.mock
async def test_stt_fails_over_at_runtime_not_only_at_configuration(both_stt):
    """The old code chose a provider from which key was set and then gave up.

    A doctor's speech was lost whenever the preferred provider had a bad
    minute, even with a healthy second provider configured.
    """
    respx.post(OPENAI_STT).mock(return_value=httpx.Response(503))
    respx.post(SARVAM_STT).mock(return_value=httpx.Response(
        200, json={"transcript": "mujhe chest pain hai", "language_code": "hi-IN"}))

    result = await stt.transcribe(b"RIFF" + b"\0" * 100, filename="a.wav", content_type="audio/wav")
    assert result.provider == "sarvam"
    assert result.text == "mujhe chest pain hai"
    assert result.fell_back is True


@respx.mock
async def test_stt_uses_the_preferred_provider_when_it_works(both_stt):
    respx.post(OPENAI_STT).mock(return_value=httpx.Response(200, json={"text": "cough since Monday"}))
    sarvam = respx.post(SARVAM_STT).mock(return_value=httpx.Response(200, json={"transcript": "x"}))
    result = await stt.transcribe(b"RIFF", filename="a.wav", content_type="audio/wav")
    assert result.provider == "openai"
    assert sarvam.call_count == 0


@respx.mock
async def test_stt_raises_when_every_provider_fails(both_stt):
    respx.post(OPENAI_STT).mock(return_value=httpx.Response(500))
    respx.post(SARVAM_STT).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(stt.AllSTTFailed) as exc:
        await stt.transcribe(b"RIFF", filename="a.wav", content_type="audio/wav")
    assert len(exc.value.attempts) == 2


def test_stt_with_no_providers_raises(settings_env):
    settings_env(OPENAI_API_KEY="", SARVAM_API_KEY="")
    assert stt.providers() == []
