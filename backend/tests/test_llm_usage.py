from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.services.llm_client import UsageCounts, calculate_cost, extract_usage
from app.services.llm_service import ConfigurationError, LLMService
from app.services.providers import ProviderResponse
from app.services.providers.cli_provider import CLIProvider


@dataclass
class _FakeAPIProvider:
    name: str = "openai"

    async def complete(self, messages, model, stream=False, **kwargs):
        del messages, kwargs
        return ProviderResponse(
            provider=self.name,
            model=model,
            text="unified response",
            usage=UsageCounts(input_tokens=100, output_tokens=20),
        )


@pytest.mark.asyncio
async def test_llm_service_routes_api_agent_without_environment_fallback():
    agent = SimpleNamespace(
        id="api-agent",
        agent_type="api",
        provider="openai",
        model="gpt-4o",
    )
    service = LLMService(api_providers={"openai": _FakeAPIProvider()})

    response = await service.complete(agent, [{"role": "user", "content": "hello"}])

    assert response.text == "unified response"
    assert response.provider == "openai"


@pytest.mark.asyncio
async def test_llm_service_rejects_missing_agent():
    with pytest.raises(ConfigurationError, match="No LLM agent configured"):
        await LLMService().complete(None, [{"role": "user", "content": "hello"}])


@pytest.mark.asyncio
async def test_llm_service_forwards_cwd_through_cli_provider_to_spawn():
    class FakeDispatcher:
        spawn_kwargs = None

        async def spawn(self, cli, model, prompt, **kwargs):
            self.spawn_kwargs = {
                "cli": cli,
                "model": model,
                "prompt": prompt,
                **kwargs,
            }
            yield '{"ok": true}'

    dispatcher = FakeDispatcher()
    service = LLMService(cli_provider=CLIProvider(dispatcher=dispatcher))
    agent = SimpleNamespace(
        id="cli-agent",
        agent_type="cli",
        provider="openai",
        cli="codex",
        model="gpt-5",
        effort="high",
    )

    response = await service.complete(
        agent,
        [{"role": "user", "content": "Read the repository."}],
        cwd="/project/repo",
    )

    assert response.text == '{"ok": true}'
    assert dispatcher.spawn_kwargs["cwd"] == "/project/repo"
    assert dispatcher.spawn_kwargs["effort"] == "high"
    assert response.usage_is_measured is False


@pytest.mark.asyncio
async def test_cli_provider_enforces_max_output_tokens():
    class FakeDispatcher:
        async def spawn(self, cli, model, prompt, **kwargs):
            del cli, model, prompt, kwargs
            yield "a" * 10
            yield "b" * 10

    agent = SimpleNamespace(
        id="cli-agent", agent_type="cli", provider="openai",
        cli="codex", model="gpt-5", effort="low",
    )
    response = await LLMService(
        cli_provider=CLIProvider(dispatcher=FakeDispatcher())
    ).complete(
        agent, [{"role": "user", "content": "bounded"}], max_tokens=3
    )

    assert response.text == "a" * 10 + "b" * 2
    assert response.usage.output_tokens <= 3


def test_usage_extraction_normalizes_anthropic_cache_tokens():
    usage = extract_usage(
        {
            "usage": {
                "input_tokens": 800,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 200,
                "output_tokens": 75,
            }
        },
        "anthropic",
    )

    assert usage.input_tokens == 1_100
    assert usage.output_tokens == 75
    assert usage.cached_tokens == 200


def test_usage_extraction_includes_google_thinking_tokens():
    usage = extract_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 600,
                "candidatesTokenCount": 90,
                "thoughtsTokenCount": 30,
                "cachedContentTokenCount": 150,
            }
        },
        "google",
    )

    assert usage.input_tokens == 600
    assert usage.output_tokens == 120
    assert usage.cached_tokens == 150


def test_cost_calculation_uses_cached_input_rate():
    cost = calculate_cost(
        "claude-3-5-sonnet-latest",
        "anthropic",
        input_tokens=1_000,
        output_tokens=100,
        cached_tokens=400,
    )

    assert cost == Decimal("0.00342000")
