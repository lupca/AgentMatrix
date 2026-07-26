"""Google Gen AI adapter for the SDK-direct coordinator."""

from __future__ import annotations

from typing import Any

from app.services.llm_client import GOOGLE_API_KEY, extract_usage
from app.services.providers import ProviderResponse, response_request_id


class GoogleAdapter:
    """Translate canonical messages to Gemini generateContent requests."""

    name = "google"

    def __init__(self, client: Any | None = None, *, api_key: str | None = None):
        self._client = client
        self._api_key = api_key if api_key is not None else GOOGLE_API_KEY

    def _get_client(self):
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "The google-genai package is required for Gemini coordinator models"
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    @staticmethod
    def render_messages(
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], str]:
        """Return Gemini contents and a stable system instruction."""

        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role", "user")
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
                continue
            if role not in {"user", "assistant"}:
                content = f"[{role} result]\n{content}"
                role = "user"
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": content}],
                }
            )
        return contents, "\n\n".join(system_parts)

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        stream: bool = False,
        *,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ProviderResponse:
        contents, system = self.render_messages(messages)
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system

        normalized = ProviderResponse(provider=self.name, model=model)
        client = self._get_client()
        if not stream:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            normalized.raw_response = response
            normalized.text = response.text or ""
            normalized.usage = extract_usage(response, self.name)
            normalized.request_id = response_request_id(response)
            normalized.stop_reason = self._stop_reason(response)
            return normalized

        async def iter_chunks():
            pieces: list[str] = []
            final_response: Any = None
            response_stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            async for chunk in response_stream:
                final_response = chunk
                text = chunk.text or ""
                if text:
                    pieces.append(text)
                    yield text
            normalized.raw_response = final_response
            normalized.text = "".join(pieces)
            normalized.usage = extract_usage(final_response or {}, self.name)
            normalized.request_id = response_request_id(final_response)
            normalized.stop_reason = self._stop_reason(final_response)

        normalized.chunks = iter_chunks()
        return normalized

    @staticmethod
    def _stop_reason(response: Any) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        return str(reason) if reason is not None else None

    async def close(self) -> None:
        if self._client is None:
            return
        aio = getattr(self._client, "aio", None)
        aclose = getattr(aio, "aclose", None)
        if aclose is not None:
            await aclose()
        close = getattr(self._client, "close", None)
        if close is not None:
            close()
        self._client = None
