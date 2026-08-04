"""CLI-backed provider used by the unified LLM service."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.services.cli_dispatcher import CLIDispatcher, route_model
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse


def _estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else str(value or "")
    return max(1, (len(text) + 3) // 4)


def _extract_cli_text(stdout: str) -> str:
    """Extract the model text from stream-json without hiding plain JSON.

    Planner and critic prompts require a JSON document. The shared CLI flags
    intentionally request JSONL telemetry, whose envelope differs by vendor;
    unwrap the last textual result before schema parsing while retaining a
    plain JSON response from test doubles or a CLI that emits one directly.
    """
    objects: list[dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            objects.append(value)
    if not objects:
        return stdout

    candidates: list[str] = []
    for value in objects:
        if isinstance(value.get("result"), str):
            candidates.append(value["result"])
        if isinstance(value.get("response"), str):
            candidates.append(value["response"])
        if isinstance(value.get("text"), str):
            candidates.append(value["text"])
        item = value.get("item")
        if isinstance(item, dict):
            if isinstance(item.get("text"), str):
                candidates.append(item["text"])
            content = item.get("content")
            if isinstance(content, list):
                candidates.extend(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
        message = value.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            candidates.extend(
                block["text"]
                for block in message["content"]
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
    return candidates[-1] if candidates else stdout


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
        # ``max_tokens`` is an API transport parameter. Subscription CLIs do
        # not expose a portable equivalent; their native timeout is applied
        # by CLIDispatcher (and agy's --print-timeout in the shared builder).
        del max_tokens, temperature, tools
        route = route_model(model, provider)
        selected_cli = cli or route.cli
        prompt = self.format_prompt(messages)
        response = ProviderResponse(
            provider=route.provider,
            model=model,
            request_id=f"cli-{uuid.uuid4()}",
            stop_reason="stop",
            usage_is_measured=False,
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
                output_tokens=output_tokens,
                cached_tokens=0,
            )

        if stream:
            response.chunks = chunks()
        else:
            parts: list[str] = []
            async for chunk in chunks():
                parts.append(chunk)
            response.text = _extract_cli_text("".join(parts))
        return response


__all__ = ["CLIProvider"]
