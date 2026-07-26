"""Anthropic Messages API adapter for the SDK-direct coordinator."""

from __future__ import annotations

from typing import Any

from app.services.llm_client import ANTHROPIC_API_KEY, extract_usage
from app.services.providers import ProviderResponse, response_request_id


class AnthropicAdapter:
    """Translate canonical messages to Claude Messages API requests."""

    name = "anthropic"

    def __init__(self, client: Any | None = None, *, api_key: str | None = None):
        self._client = client
        self._api_key = api_key if api_key is not None else ANTHROPIC_API_KEY

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "The anthropic package is required for Claude coordinator models"
                ) from exc
            # CoordinatorService owns turn retries, so avoid multiplying them by
            # the SDK's built-in retry loop.
            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key,
                max_retries=0,
            )
        return self._client

    @staticmethod
    def render_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return cacheable system blocks and Claude user/assistant messages."""

        system_parts: list[dict[str, Any]] = []
        provider_messages: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", ""))
            cache_ctrl = message.get("cache_control")
            if role == "system":
                block: dict[str, Any] = {"type": "text", "text": content}
                if cache_ctrl:
                    block["cache_control"] = cache_ctrl
                system_parts.append(block)
                continue
            if role not in {"user", "assistant"}:
                content = f"[{role} result]\n{content}"
                role = "user"
            
            if cache_ctrl:
                msg = {
                    "role": role,
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": cache_ctrl,
                        }
                    ],
                }
            else:
                msg = {"role": role, "content": content}
            provider_messages.append(msg)

        system = [
            {
                "type": "text",
                "text": block["text"],
                "cache_control": block.get("cache_control", {"type": "ephemeral"}),
            }
            for block in system_parts
        ]
        return system, provider_messages

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = False,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ProviderResponse:
        system, provider_messages = self.render_messages(messages)
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": provider_messages,
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            request["system"] = system

        normalized = ProviderResponse(provider=self.name, model=model)
        client = self._get_client()
        if not stream:
            response = await client.messages.create(**request)
            normalized.raw_response = response
            normalized.text = "".join(
                str(getattr(block, "text", ""))
                for block in (getattr(response, "content", None) or [])
                if getattr(block, "type", "text") == "text"
            )
            normalized.usage = extract_usage(response, self.name)
            normalized.request_id = response_request_id(response)
            normalized.stop_reason = getattr(response, "stop_reason", None)
            return normalized

        async def iter_chunks():
            pieces: list[str] = []
            async with client.messages.stream(**request) as response_stream:
                async for text in response_stream.text_stream:
                    if text:
                        pieces.append(text)
                        yield text
                final_response = await response_stream.get_final_message()
            normalized.raw_response = final_response
            normalized.text = "".join(pieces)
            normalized.usage = extract_usage(final_response, self.name)
            normalized.request_id = response_request_id(final_response)
            normalized.stop_reason = getattr(final_response, "stop_reason", None)

        normalized.chunks = iter_chunks()
        return normalized

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            self._client = None
