from types import SimpleNamespace

import pytest

from app.services.providers.openai_adapter import OpenAIAdapter


class _Completions:
    def __init__(self):
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)
        if request.get("stream"):
            return _Stream(
                [
                    SimpleNamespace(
                        id="stream-request",
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="Hello ",
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id="call-stream",
                                            function=SimpleNamespace(name="status", arguments='{"sta'),
                                        )
                                    ],
                                ),
                                finish_reason=None,
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        id="stream-request",
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(
                                    content="world",
                                    tool_calls=[
                                        SimpleNamespace(
                                            index=0,
                                            id=None,
                                            function=SimpleNamespace(name=None, arguments='tus":true}'),
                                        )
                                    ],
                                ),
                                finish_reason="stop",
                            )
                        ],
                        usage=None,
                    ),
                    SimpleNamespace(
                        id="stream-request",
                        choices=[],
                        usage=SimpleNamespace(
                            prompt_tokens=80,
                            completion_tokens=12,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=20),
                        ),
                    ),
                ]
            )
        return SimpleNamespace(
            id="response-id",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="Hello from OpenAI"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=25,
                prompt_tokens_details=SimpleNamespace(cached_tokens=30),
            ),
        )


class _OpenAIClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_Completions())


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk


def test_render_messages_and_tools_converts_canonical_shapes():
    messages = OpenAIAdapter.render_messages(
        [
            {"role": "system", "content": "Rules", "cache_control": {"type": "ephemeral"}},
            {"role": "user", "content": "Hello", "status": "complete"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "name": "status", "input": {}}]},
            {"role": "tool", "content": "42", "tool_call_id": "call-1", "name": "status"},
            {"role": "tool", "content": "unattached"},
        ]
    )

    assert messages == [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": "Hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "status", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "42", "tool_call_id": "call-1", "name": "status"},
        {"role": "user", "content": "[tool result]\nunattached"},
    ]

    tools = OpenAIAdapter.render_tools(
        [
            {"name": "status", "description": "Read status", "input_schema": {"type": "object"}},
            {"type": "tool_search_tool_regex_20251119", "name": "tool_search"},
        ]
    )
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "status",
                "description": "Read status",
                "parameters": {"type": "object"},
            },
        }
    ]


@pytest.mark.asyncio
async def test_complete_extracts_text_usage_and_request_options():
    client = _OpenAIClient()
    adapter = OpenAIAdapter(api_key="test-key", client=client)

    response = await adapter.complete(
        [{"role": "system", "content": "Rules"}, {"role": "user", "content": "Hi"}],
        "gpt-4o",
        max_tokens=512,
        temperature=0.2,
    )

    assert response.text == "Hello from OpenAI"
    assert response.request_id == "response-id"
    assert response.stop_reason == "stop"
    assert response.tool_calls is None
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 25
    assert response.usage.cached_tokens == 30
    assert client.chat.completions.requests[0]["max_tokens"] == 512
    assert client.chat.completions.requests[0]["temperature"] == 0.2


@pytest.mark.asyncio
async def test_complete_normalizes_streaming_and_usage():
    client = _OpenAIClient()
    adapter = OpenAIAdapter(api_key="test-key", client=client)

    response = await adapter.complete([{"role": "user", "content": "Hi"}], "gpt-4o", stream=True)
    chunks = [chunk async for chunk in response.chunks]

    assert chunks == ["Hello ", "world"]
    assert response.text == "Hello world"
    assert response.request_id == "stream-request"
    assert response.stop_reason == "stop"
    assert response.tool_calls == [
        {"id": "call-stream", "name": "status", "input": {"status": True}}
    ]
    assert response.usage.input_tokens == 80
    assert response.usage.output_tokens == 12
    assert response.usage.cached_tokens == 20
    assert client.chat.completions.requests[0]["stream_options"] == {"include_usage": True}


def test_tool_calls_extracts_dict_and_object_response_shapes():
    assert OpenAIAdapter._tool_calls(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call-dict",
                                "function": {"name": "status", "arguments": '{"verbose":true}'},
                            }
                        ]
                    }
                }
            ]
        }
    ) == [{"id": "call-dict", "name": "status", "input": {"verbose": True}}]

    assert OpenAIAdapter._tool_calls(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                id="call-object",
                                function=SimpleNamespace(name="status", arguments="{}"),
                            )
                        ]
                    )
                )
            ]
        )
    ) == [{"id": "call-object", "name": "status", "input": {}}]


def test_text_combines_reasoning_and_content_with_think_tags():
    assert OpenAIAdapter._text(
        {"choices": [{"message": {"reasoning_content": "reason", "content": "answer"}}]}
    ) == "<think>reason</think>answer"
    assert OpenAIAdapter._text(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(reasoning_content="reason", content="answer")
                )
            ]
        )
    ) == "<think>reason</think>answer"


def test_chunk_text_combines_reasoning_and_content_with_think_tags():
    assert OpenAIAdapter._chunk_text(
        {"choices": [{"delta": {"reasoning_content": "reason", "content": "answer"}}]}
    ) == "<think>reason</think>answer"
    assert OpenAIAdapter._chunk_text(
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(reasoning_content="reason", content="answer")
                )
            ]
        )
    ) == "<think>reason</think>answer"


@pytest.mark.asyncio
async def test_complete_propagates_openai_api_errors():
    class RateLimitError(RuntimeError):
        status_code = 429

    class ErrorCompletions:
        async def create(self, **_request):
            raise RateLimitError("rate limit")

    client = SimpleNamespace(chat=SimpleNamespace(completions=ErrorCompletions()))
    adapter = OpenAIAdapter(api_key="test-key", client=client)

    with pytest.raises(RateLimitError):
        await adapter.complete([{"role": "user", "content": "Hi"}], "gpt-4o")
