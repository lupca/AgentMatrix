"""OpenAI Chat Completions adapter for the SDK-direct coordinator."""

from __future__ import annotations

from typing import Any

from app.services.llm_client import OPENAI_API_KEY, extract_usage
from app.services.providers import ProviderResponse, response_request_id


class OpenAIAdapter:
    """Translate canonical messages to OpenAI Chat Completions requests."""

    name = "openai"

    def __init__(self, client: Any | None = None, *, api_key: str | None = None):
        self._client = client
        self._api_key = api_key if api_key is not None else OPENAI_API_KEY

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "The openai package is required for OpenAI coordinator models"
                ) from exc
            # CoordinatorService owns turn retries, so avoid multiplying them by
            # the SDK's built-in retry loop.
            self._client = AsyncOpenAI(api_key=self._api_key, max_retries=0)
        return self._client

    @staticmethod
    def render_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert canonical messages to the OpenAI chat message shape.

        OpenAI uses the same core roles as the canonical representation. The
        adapter deliberately drops Control Tower metadata such as cache
        markers, IDs, and status fields because those are not API message
        properties. Tool results without a call ID are rendered as user text;
        OpenAI rejects a bare ``tool`` message without ``tool_call_id``.
        """

        rendered: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content", "")
            if not isinstance(content, str):
                content = str(content)

            if role not in {"system", "user", "assistant", "tool"}:
                content = f"[{role} result]\n{content}"
                role = "user"
            elif role == "tool" and not message.get("tool_call_id"):
                content = f"[tool result]\n{content}"
                role = "user"

            item: dict[str, Any] = {"role": role, "content": content}
            if role == "tool":
                item["tool_call_id"] = str(message["tool_call_id"])
                if message.get("name"):
                    item["name"] = str(message["name"])
            elif role == "assistant" and message.get("tool_calls"):
                item["tool_calls"] = OpenAIAdapter._render_tool_calls(
                    message["tool_calls"]
                )
            rendered.append(item)
        return rendered

    @staticmethod
    def _render_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
        """Normalize Anthropic- or OpenAI-shaped assistant tool calls."""

        if not isinstance(tool_calls, list):
            return []
        rendered: list[dict[str, Any]] = []
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                name = function.get("name", "")
                arguments = function.get("arguments", "{}")
            else:
                name = call.get("name", "")
                arguments = call.get("arguments")
                if arguments is None:
                    arguments = call.get("input", {})
            if not isinstance(arguments, str):
                import json

                arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            rendered.append(
                {
                    "id": str(call.get("id", "")),
                    "type": "function",
                    "function": {"name": str(name), "arguments": arguments},
                }
            )
        return rendered

    @staticmethod
    def render_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Convert Anthropic-style tool schemas to OpenAI function tools."""

        rendered: list[dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                function = tool["function"]
                rendered.append(
                    {
                        "type": "function",
                        "function": {
                            key: function[key]
                            for key in ("name", "description", "parameters")
                            if key in function
                        },
                    }
                )
                continue
            # Anthropic's deferred tool-search declaration has no OpenAI
            # equivalent, so omit it instead of sending an invalid schema.
            if not tool.get("name") or "input_schema" not in tool:
                continue
            function = {
                "name": tool["name"],
                "parameters": tool["input_schema"],
            }
            if tool.get("description"):
                function["description"] = tool["description"]
            rendered.append({"type": "function", "function": function})
        return rendered

    @staticmethod
    def _text(response: Any) -> str:
        choices = response.get("choices", []) if isinstance(response, dict) else getattr(response, "choices", [])
        if not choices:
            return ""
        choice = choices[0]
        message = choice.get("message", {}) if isinstance(choice, dict) else getattr(choice, "message", None)
        if isinstance(message, dict):
            return str(message.get("content") or message.get("reasoning_content") or "")
        return str(
            getattr(message, "content", None)
            or getattr(message, "reasoning_content", None)
            or ""
        )

    @staticmethod
    def _stop_reason(response: Any) -> str | None:
        choices = response.get("choices", []) if isinstance(response, dict) else getattr(response, "choices", [])
        if not choices:
            return None
        choice = choices[0]
        reason = choice.get("finish_reason") if isinstance(choice, dict) else getattr(choice, "finish_reason", None)
        return str(reason) if reason is not None else None

    @staticmethod
    def _request(model: str, max_tokens: int, temperature: float) -> dict[str, Any]:
        request: dict[str, Any] = {"model": model}
        # Reasoning models reject temperature and use the newer token limit
        # parameter. GPT chat models retain the standard Chat Completions API.
        if model.lower().startswith(("o1-", "o3-", "o4-")):
            request["max_completion_tokens"] = max_tokens
        else:
            request["max_tokens"] = max_tokens
            request["temperature"] = temperature
        return request

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = False,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        request = self._request(model, max_tokens, temperature)
        request["messages"] = self.render_messages(messages)
        rendered_tools = self.render_tools(tools)
        if rendered_tools:
            request["tools"] = rendered_tools

        normalized = ProviderResponse(provider=self.name, model=model)
        client = self._get_client()
        if not stream:
            response = await client.chat.completions.create(**request)
            normalized.raw_response = response
            normalized.text = self._text(response)
            normalized.usage = extract_usage(response, self.name)
            normalized.request_id = response_request_id(response)
            normalized.stop_reason = self._stop_reason(response)
            return normalized

        request["stream"] = True
        request["stream_options"] = {"include_usage": True}

        async def iter_chunks():
            pieces: list[str] = []
            final_chunk: Any = None
            usage_response: Any = None
            async for chunk in await client.chat.completions.create(**request):
                final_chunk = chunk
                if response_request_id(chunk) and not normalized.request_id:
                    normalized.request_id = response_request_id(chunk)
                usage = getattr(chunk, "usage", None)
                if usage is None and isinstance(chunk, dict):
                    usage = chunk.get("usage")
                if usage is not None:
                    usage_response = {"usage": usage}
                text = self._chunk_text(chunk)
                if text:
                    pieces.append(text)
                    yield text
                stop_reason = self._stop_reason(chunk)
                if stop_reason is not None:
                    normalized.stop_reason = stop_reason
            normalized.raw_response = final_chunk
            normalized.text = "".join(pieces)
            normalized.usage = extract_usage(usage_response or final_chunk or {}, self.name)

        normalized.chunks = iter_chunks()
        return normalized

    @staticmethod
    def _chunk_text(chunk: Any) -> str:
        choices = chunk.get("choices", []) if isinstance(chunk, dict) else getattr(chunk, "choices", [])
        if not choices:
            return ""
        choice = choices[0]
        delta = choice.get("delta", {}) if isinstance(choice, dict) else getattr(choice, "delta", None)
        if isinstance(delta, dict):
            return str(delta.get("content") or delta.get("reasoning_content") or "")
        return str(
            getattr(delta, "content", None)
            or getattr(delta, "reasoning_content", None)
            or ""
        )

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        self._client = None
