"""API-backed provider for agents using an OpenAI-compatible endpoint."""

from __future__ import annotations

from app.services.providers.openai_adapter import OpenAIAdapter


class APIProvider(OpenAIAdapter):
    """OpenAI-compatible API provider used by :class:`LLMService`.

    ``OpenAIAdapter`` remains the low-level request/normalization
    implementation so existing message and tool handling stays in one place.
    The public provider name is now transport-oriented rather than tied to a
    particular vendor.
    """

    name = "openai"


__all__ = ["APIProvider"]
