from decimal import Decimal

import pytest

from app.db.models import LLMUsage
from app.services.llm_client import LLMClient, calculate_cost, extract_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, *_args, **_kwargs):
        return _FakeResponse(self.payload)


class _FakeStreamResponse:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"streamed"}}]}'
        yield (
            'data: {"choices":[],"usage":{"prompt_tokens":120,'
            '"completion_tokens":30,"prompt_tokens_details":{"cached_tokens":20}}}'
        )
        yield "data: [DONE]"


class _FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, *_args, **_kwargs):
        return _FakeStreamResponse()


def test_siliconflow_completion_persists_usage(db_session, monkeypatch):
    payload = {
        "choices": [{"message": {"content": "Telemetry works"}}],
        "usage": {
            "prompt_tokens": 1_000,
            "completion_tokens": 250,
            "prompt_tokens_details": {"cached_tokens": 100},
        },
    }
    client = LLMClient(provider="siliconflow", db_session=db_session)
    monkeypatch.setattr(
        client,
        "_get_siliconflow_client",
        lambda: _FakeClient(payload),
    )

    result = client.complete(
        [{"role": "user", "content": "hello"}],
        operation="plan",
    )

    assert result == "Telemetry works"
    record = db_session.query(LLMUsage).one()
    assert record.provider == "siliconflow"
    assert record.model == "moonshotai/Kimi-K3"
    assert record.operation == "plan"
    assert record.input_tokens == 1_000
    assert record.output_tokens == 250
    assert record.cached_tokens == 100
    assert record.cost_usd == Decimal("0.00122500")
    assert record.latency_ms >= 0


@pytest.mark.asyncio
async def test_siliconflow_stream_persists_final_usage(db_session, monkeypatch):
    client = LLMClient(provider="siliconflow", db_session=db_session)
    monkeypatch.setattr(
        client,
        "_get_async_siliconflow_client",
        lambda: _FakeAsyncClient(),
    )

    chunks = [
        chunk
        async for chunk in client.stream_async(
            [{"role": "user", "content": "hello"}],
            operation="chat",
        )
    ]

    assert chunks == ["streamed"]
    record = db_session.query(LLMUsage).one()
    assert record.operation == "chat"
    assert record.input_tokens == 120
    assert record.output_tokens == 30
    assert record.cached_tokens == 20


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

    # 600 * $3/MTok + 400 * $0.30/MTok + 100 * $15/MTok
    assert cost == Decimal("0.00342000")
