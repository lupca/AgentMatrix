from types import SimpleNamespace

import pytest

from app.services.providers.anthropic_adapter import AnthropicAdapter
from app.services.providers.google_adapter import GoogleAdapter


class _AnthropicMessages:
    def __init__(self):
        self.request = None

    async def create(self, **request):
        self.request = request
        return SimpleNamespace(
            id="anthropic-response",
            _request_id="request-anthropic",
            content=[SimpleNamespace(type="text", text="Claude reply")],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=100,
                output_tokens=20,
                cache_read_input_tokens=40,
                cache_creation_input_tokens=10,
            ),
        )

    def stream(self, **request):
        self.request = request
        return _AnthropicStream()


class _TextStream:
    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        yield "Claude "
        yield "stream"


class _AnthropicStream:
    text_stream = _TextStream()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_final_message(self):
        return SimpleNamespace(
            id="anthropic-stream-response",
            content=[],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=30,
                output_tokens=5,
                cache_read_input_tokens=10,
                cache_creation_input_tokens=0,
            ),
        )


class _AnthropicClient:
    def __init__(self):
        self.messages = _AnthropicMessages()


@pytest.mark.asyncio
async def test_anthropic_adapter_translates_messages_and_usage():
    client = _AnthropicClient()
    adapter = AnthropicAdapter(client=client)

    response = await adapter.complete(
        [
            {"role": "system", "content": "Stable instructions"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "tool", "content": "tool output"},
        ],
        "claude-sonnet-4",
    )

    assert response.text == "Claude reply"
    assert response.request_id == "request-anthropic"
    assert response.usage.input_tokens == 150
    assert response.usage.cached_tokens == 40
    request = client.messages.request
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["system"][0]["text"] == "Stable instructions"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert request["messages"][-1] == {
        "role": "user",
        "content": "[tool result]\ntool output",
    }


@pytest.mark.asyncio
async def test_anthropic_adapter_normalizes_streaming():
    adapter = AnthropicAdapter(client=_AnthropicClient())

    response = await adapter.complete(
        [{"role": "user", "content": "Hello"}],
        "claude-sonnet-4",
        stream=True,
    )
    chunks = [chunk async for chunk in response.chunks]

    assert chunks == ["Claude ", "stream"]
    assert response.text == "Claude stream"
    assert response.usage.input_tokens == 40
    assert response.usage.cached_tokens == 10


class _GoogleModels:
    def __init__(self):
        self.request = None

    async def generate_content(self, **request):
        self.request = request
        return SimpleNamespace(
            text="Gemini reply",
            id="google-response",
            candidates=[SimpleNamespace(finish_reason="STOP")],
            usage_metadata=SimpleNamespace(
                prompt_token_count=80,
                candidates_token_count=15,
                thoughts_token_count=5,
                cached_content_token_count=25,
            ),
        )

    async def generate_content_stream(self, **request):
        self.request = request

        async def chunks():
            yield SimpleNamespace(
                text="Gemini ",
                id="google-stream-1",
                candidates=[],
                usage_metadata=None,
            )
            yield SimpleNamespace(
                text="stream",
                id="google-stream-2",
                candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=60,
                    candidates_token_count=12,
                    thoughts_token_count=3,
                    cached_content_token_count=20,
                ),
            )

        return chunks()


class _GoogleClient:
    def __init__(self):
        self.aio = SimpleNamespace(models=_GoogleModels())


@pytest.mark.asyncio
async def test_google_adapter_translates_messages_and_usage():
    client = _GoogleClient()
    adapter = GoogleAdapter(client=client)

    response = await adapter.complete(
        [
            {"role": "system", "content": "Stable instructions"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ],
        "gemini-2.5-flash",
    )

    assert response.text == "Gemini reply"
    assert response.usage.input_tokens == 80
    assert response.usage.output_tokens == 20
    assert response.usage.cached_tokens == 25
    request = client.aio.models.request
    assert request["config"]["system_instruction"] == "Stable instructions"
    assert request["contents"] == [
        {"role": "user", "parts": [{"text": "Hello"}]},
        {"role": "model", "parts": [{"text": "Hi"}]},
    ]


@pytest.mark.asyncio
async def test_google_adapter_normalizes_streaming():
    adapter = GoogleAdapter(client=_GoogleClient())

    response = await adapter.complete(
        [{"role": "user", "content": "Hello"}],
        "gemini-2.5-flash",
        stream=True,
    )
    chunks = [chunk async for chunk in response.chunks]

    assert chunks == ["Gemini ", "stream"]
    assert response.text == "Gemini stream"
    assert response.usage.output_tokens == 15
    assert response.usage.cached_tokens == 20


def test_provider_renderers_preserve_tool_results_as_portable_text():
    messages = [{"role": "tool", "content": "42"}]

    _, anthropic_messages = AnthropicAdapter.render_messages(messages)
    google_messages, _ = GoogleAdapter.render_messages(messages)

    assert anthropic_messages[0]["content"] == "[tool result]\n42"
    assert google_messages[0]["parts"][0]["text"] == "[tool result]\n42"
