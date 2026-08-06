"""The single entry point for model calls made by the application."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.db.models import Agent
from app.services.crypto import decrypt_api_key
from app.services.providers import CoordinatorProvider, ProviderResponse
from app.services.providers.api_provider import APIProvider
from app.services.providers.cli_provider import CLIProvider


class ConfigurationError(ValueError):
    """Raised when an LLM call has no complete agent configuration."""


def provider_name_for_model(model: str) -> str:
    """Infer a provider for API validation and session selection only."""

    normalized = (model or "").strip().lower()
    if "claude" in normalized:
        return "anthropic"
    if "gemini" in normalized:
        return "google"
    if normalized.startswith(("gpt-", "o1-", "o3-", "o4-", "chatgpt-")):
        return "openai"
    raise ConfigurationError(
        f"Cannot infer provider for model '{model}'. Specify a configured agent."
    )


class LLMService:
    """Route every model call through an explicitly configured agent."""

    def __init__(
        self,
        *,
        api_providers: Mapping[str, CoordinatorProvider] | None = None,
        cli_provider: CLIProvider | None = None,
    ) -> None:
        self.api_providers = dict(api_providers or {})
        self.cli_provider = cli_provider or CLIProvider()

    @staticmethod
    def _agent_type(agent: Any) -> str:
        value = getattr(agent, "agent_type", None)
        return getattr(value, "value", value) or ""

    def _provider_for(self, agent: Agent | Any) -> CoordinatorProvider:
        if agent is None:
            raise ConfigurationError(
                "No LLM agent configured. Select an agent before making a model call."
            )

        agent_type = self._agent_type(agent).lower()
        if agent_type == "api":
            provider_name = str(getattr(agent, "provider", "") or "").lower()
            if not provider_name:
                raise ConfigurationError(
                    f"API agent '{getattr(agent, 'id', '<unknown>')}' has no provider."
                )
            injected = self.api_providers.get(provider_name)
            if injected is not None:
                return injected
            encrypted_key = getattr(agent, "api_key", None)
            if not encrypted_key:
                raise ConfigurationError(
                    f"API agent '{getattr(agent, 'id', '<unknown>')}' has no API key."
                )
            try:
                api_key = decrypt_api_key(encrypted_key)
            except ValueError as exc:
                raise ConfigurationError(
                    f"API agent '{getattr(agent, 'id', '<unknown>')}' has an invalid API key."
                ) from exc
            return APIProvider(api_key=api_key, base_url=getattr(agent, "base_url", None))

        if agent_type == "cli":
            if not getattr(agent, "cli", None):
                raise ConfigurationError(
                    f"CLI agent '{getattr(agent, 'id', '<unknown>')}' has no CLI configured."
                )
            return self.cli_provider

        raise ConfigurationError(
            f"Agent '{getattr(agent, 'id', '<unknown>')}' has unsupported type '{agent_type}'."
        )

    @staticmethod
    def _model_for(agent: Any, model: str | None) -> str:
        selected = model or getattr(agent, "model", None)
        if not selected or not str(selected).strip():
            raise ConfigurationError(
                f"Agent '{getattr(agent, 'id', '<unknown>')}' has no model configured."
            )
        return str(selected)

    async def complete(
        self,
        agent: Agent | Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        stream: bool = False,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        cwd: str | None = None,
        on_start: Any = None,
        on_heartbeat: Any = None,
        timeout_seconds: int | None = None,
    ) -> ProviderResponse:
        """Complete a request through the provider selected by ``agent``.

        ``on_start``, ``on_heartbeat`` and ``timeout_seconds`` are only ever
        forwarded to a CLI-backed provider -- an API provider has no
        subprocess to report a PID for, no liveness tick, and its own
        transport deadline, so it never receives them.

        Pass ``timeout_seconds`` whenever the caller holds an AgentRun: the
        dispatcher's own default is 4 hours and it does not know about the
        row (CTV2-1410).
        """

        provider = self._provider_for(agent)
        selected_model = self._model_for(agent, model)
        kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": tools,
        }
        if self._agent_type(agent).lower() == "cli":
            kwargs.update(
                provider=getattr(agent, "provider", None),
                cli=getattr(agent, "cli", None),
                effort=getattr(agent, "effort", None),
                cwd=cwd,
                on_start=on_start,
                on_heartbeat=on_heartbeat,
                timeout_seconds=timeout_seconds,
            )
        return await provider.complete(
            messages,
            selected_model,
            stream,
            **kwargs,
        )

    async def stream(
        self,
        agent: Agent | Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Yield text from a provider stream."""

        response = await self.complete(
            agent,
            messages,
            tools,
            model=model,
            stream=True,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if response.chunks is not None:
            async for chunk in response.chunks:
                yield chunk
        elif response.text:
            yield response.text

    def complete_sync(
        self,
        agent: Agent | Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Synchronous bridge for the synchronous context-compaction API."""

        def run() -> ProviderResponse:
            return asyncio.run(self.complete(agent, messages, tools, **kwargs))

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(run).result()


__all__ = ["ConfigurationError", "LLMService", "provider_name_for_model"]
