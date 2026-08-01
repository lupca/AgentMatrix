"""CLI-backed provider used by the unified LLM service."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.services.cli_dispatcher import CLIDispatcher, route_model
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else str(value or "")
    return max(1, (len(text) + 3) // 4)


class CLIProvider:
    """Adapt an account-backed CLI process to the provider interface."""

    name = "cli"

    def __init__(self, dispatcher: CLIDispatcher | None = None):
        self.dispatcher = dispatcher or CLIDispatcher()

    @staticmethod
    def format_prompt(messages: list[dict[str, Any]]) -> str:
        from app.services.cli_dispatcher import format_history_as_prompt

        return format_history_as_prompt(messages)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = False,
        *,
        provider: str | None = None,
        cli: str | None = None,
        effort: str | None = None,
        cwd: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderResponse:
        del max_tokens, temperature, tools
        route = route_model(model, provider)
        selected_cli = cli or route.cli
        prompt = self.format_prompt(messages)
        response = ProviderResponse(
            provider=route.provider,
            model=model,
            request_id=f"cli-{uuid.uuid4()}",
            stop_reason="stop",
            usage=UsageCounts(
                input_tokens=sum(_estimate_tokens(item.get("content", "")) for item in messages),
                output_tokens=0,
            ),
        )

        async def chunks() -> AsyncIterator[str]:
            output_tokens = 0
            spawn_kwargs: dict[str, Any] = {"effort": effort}
            if cwd is not None:
                spawn_kwargs["cwd"] = cwd
            async for chunk in self.dispatcher.spawn(
                selected_cli, model, prompt, **spawn_kwargs
            ):
                output_tokens += _estimate_tokens(chunk)
                yield chunk
            response.usage = UsageCounts(
                input_tokens=response.usage.input_tokens,
                output_tokens=max(0, output_tokens - 1),
                cached_tokens=0,
            )

        if stream:
            response.chunks = chunks()
        else:
            parts: list[str] = []
            async for chunk in chunks():
                parts.append(chunk)
            response.text = "".join(parts)
        return response


__all__ = ["CLIProvider"]
