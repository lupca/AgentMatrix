"""Provider-neutral interfaces for SDK-direct coordinator model calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from app.services.llm_client import UsageCounts


@dataclass
class ProviderResponse:
    """Normalized response returned by coordinator provider adapters.

    For streaming calls, ``chunks`` is populated and the remaining fields are
    updated by the adapter as the stream is consumed.
    """

    provider: str
    model: str
    text: str = ""
    usage: UsageCounts = field(default_factory=UsageCounts)
    request_id: str | None = None
    stop_reason: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    raw_response: Any = None
    chunks: AsyncIterator[str] | None = None


class CoordinatorProvider(Protocol):
    """Common async interface implemented by coordinator providers."""

    name: str

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
        """Complete a canonical message list, optionally as normalized chunks."""


def response_request_id(response: Any) -> str | None:
    """Read a request ID across SDK response shapes."""

    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("request_id") or response.get("id")
    return getattr(response, "_request_id", None) or getattr(response, "id", None)


from app.services.providers.openai_adapter import OpenAIAdapter  # noqa: E402


__all__ = [
    "CoordinatorProvider",
    "OpenAIAdapter",
    "ProviderResponse",
    "response_request_id",
]
