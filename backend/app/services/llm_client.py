"""Unified LLM client with durable token, cost, and latency telemetry."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from time import perf_counter
from typing import Any, AsyncIterator

import httpx
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import LLMUsage


logger = logging.getLogger(__name__)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "siliconflow").lower()

SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1")
SILICONFLOW_MODEL = os.getenv("SILICONFLOW_MODEL", "moonshotai/Kimi-K3")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")


@dataclass(frozen=True)
class UsageCounts:
    """Normalized provider usage. Cached tokens are a subset of input tokens."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class ModelPricing:
    """USD price per one million tokens."""

    input: Decimal
    output: Decimal
    cached_input: Decimal


@dataclass(frozen=True)
class _ProviderResult:
    text: str
    response: Any


# Prices are explicit and version-controlled so historical ledger rows remain
# reproducible. Unknown models use the provider fallback below instead of being
# silently recorded at zero cost.
MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-3-5-sonnet": ModelPricing(Decimal("3.00"), Decimal("15.00"), Decimal("0.30")),
    "claude-3-7-sonnet": ModelPricing(Decimal("3.00"), Decimal("15.00"), Decimal("0.30")),
    "claude-sonnet-4": ModelPricing(Decimal("3.00"), Decimal("15.00"), Decimal("0.30")),
    "claude-3-5-haiku": ModelPricing(Decimal("0.80"), Decimal("4.00"), Decimal("0.08")),
    "claude-3-haiku": ModelPricing(Decimal("0.25"), Decimal("1.25"), Decimal("0.03")),
    "gemini-2.5-flash-lite": ModelPricing(Decimal("0.10"), Decimal("0.40"), Decimal("0.01")),
    "gemini-2.5-flash": ModelPricing(Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),
    "gemini-2.5-pro": ModelPricing(Decimal("1.25"), Decimal("10.00"), Decimal("0.125")),
    "gemini-3.5-flash": ModelPricing(Decimal("1.50"), Decimal("9.00"), Decimal("0.15")),
    "moonshotai/kimi-k3": ModelPricing(Decimal("0.60"), Decimal("2.50"), Decimal("0.60")),
    "moonshotai/kimi-k2": ModelPricing(Decimal("0.60"), Decimal("2.50"), Decimal("0.60")),
}

PROVIDER_FALLBACK_PRICING: dict[str, ModelPricing] = {
    "anthropic": ModelPricing(Decimal("3.00"), Decimal("15.00"), Decimal("0.30")),
    "google": ModelPricing(Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),
    "siliconflow": ModelPricing(Decimal("0.60"), Decimal("2.50"), Decimal("0.60")),
    "openai": ModelPricing(Decimal("1.00"), Decimal("4.00"), Decimal("0.50")),
}


def _read_value(source: Any, *names: str, default: Any = 0) -> Any:
    if source is None:
        return default
    for name in names:
        if isinstance(source, dict) and name in source:
            value = source[name]
        else:
            value = getattr(source, name, None)
        if value is not None:
            return value
    return default


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extract_usage(response: Any, provider: str) -> UsageCounts:
    """Extract and normalize usage from Anthropic, Gemini, or OpenAI-style responses."""

    provider = provider.lower()
    if provider == "google":
        usage = _read_value(response, "usage_metadata", "usageMetadata", default=None)
        if usage is None:
            usage = response
        cached = _nonnegative_int(
            _read_value(usage, "cached_content_token_count", "cachedContentTokenCount")
        )
        prompt = _nonnegative_int(
            _read_value(usage, "prompt_token_count", "promptTokenCount", "input_tokens")
        )
        candidates = _nonnegative_int(
            _read_value(usage, "candidates_token_count", "candidatesTokenCount", "output_tokens")
        )
        thoughts = _nonnegative_int(
            _read_value(usage, "thoughts_token_count", "thoughtsTokenCount")
        )
        return UsageCounts(
            input_tokens=max(prompt, cached),
            output_tokens=candidates + thoughts,
            cached_tokens=min(cached, max(prompt, cached)),
        )

    usage = _read_value(response, "usage", default=None)
    if usage is None:
        usage = response

    if provider == "anthropic":
        uncached = _nonnegative_int(_read_value(usage, "input_tokens", "inputTokens"))
        cache_read = _nonnegative_int(
            _read_value(usage, "cache_read_input_tokens", "cacheReadInputTokens")
        )
        cache_creation = _nonnegative_int(
            _read_value(usage, "cache_creation_input_tokens", "cacheCreationInputTokens")
        )
        return UsageCounts(
            input_tokens=uncached + cache_read + cache_creation,
            output_tokens=_nonnegative_int(_read_value(usage, "output_tokens", "outputTokens")),
            cached_tokens=cache_read,
        )

    prompt = _nonnegative_int(
        _read_value(usage, "prompt_tokens", "promptTokens", "input_tokens", "inputTokens")
    )
    output = _nonnegative_int(
        _read_value(
            usage,
            "completion_tokens",
            "completionTokens",
            "output_tokens",
            "outputTokens",
        )
    )
    details = _read_value(usage, "prompt_tokens_details", "promptTokensDetails", default=None)
    cached = _nonnegative_int(_read_value(details, "cached_tokens", "cachedTokens"))
    return UsageCounts(prompt, output, min(cached, prompt))


def get_model_pricing(model: str, provider: str) -> ModelPricing:
    normalized = (model or "").lower()
    for model_key in sorted(MODEL_PRICING, key=len, reverse=True):
        if model_key in normalized:
            return MODEL_PRICING[model_key]
    return PROVIDER_FALLBACK_PRICING.get(
        provider.lower(),
        ModelPricing(Decimal("1.00"), Decimal("4.00"), Decimal("0.50")),
    )


def calculate_cost(
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
) -> Decimal:
    """Calculate request cost in USD, with cached input billed separately."""

    pricing = get_model_pricing(model, provider)
    total_input = _nonnegative_int(input_tokens)
    cached_input = min(_nonnegative_int(cached_tokens), total_input)
    uncached_input = total_input - cached_input
    cost = (
        Decimal(uncached_input) * pricing.input
        + Decimal(cached_input) * pricing.cached_input
        + Decimal(_nonnegative_int(output_tokens)) * pricing.output
    ) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


class LLMClient:
    """Unified provider wrapper that records each successful LLM request."""

    def __init__(self, provider: str | None = None, db_session: Session | None = None):
        self.provider = (provider or LLM_PROVIDER).lower()
        self.db_session = db_session

    def _get_siliconflow_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=SILICONFLOW_BASE_URL,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def _get_async_siliconflow_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=SILICONFLOW_BASE_URL,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    def _get_google_client(self):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError(
                "The google-genai package is required when LLM_PROVIDER=google"
            ) from exc
        return genai.Client(api_key=GOOGLE_API_KEY)

    @staticmethod
    def _model_for_provider(provider: str, model: str | None) -> str:
        if model:
            return model
        if provider == "siliconflow":
            return SILICONFLOW_MODEL
        if provider == "anthropic":
            return ANTHROPIC_MODEL
        if provider == "google":
            return GOOGLE_MODEL
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def _persist_usage(
        self,
        *,
        response: Any,
        model: str,
        latency_ms: int,
        operation: str,
        session_id: str | None,
        task_id: str | None,
        agent_run_id: str | None,
        db_session: Session | None,
    ) -> None:
        usage = extract_usage(response, self.provider)
        record = LLMUsage(
            session_id=session_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            model=model,
            provider=self.provider,
            operation=operation,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=calculate_cost(
                model,
                self.provider,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
            ),
            latency_ms=max(0, latency_ms),
        )

        database = db_session or self.db_session
        owns_session = database is None
        if database is None:
            database = SessionLocal()

        try:
            database.add(record)
            database.commit()
        except Exception:
            database.rollback()
            logger.exception(
                "Failed to persist LLM usage for provider=%s model=%s operation=%s",
                self.provider,
                model,
                operation,
            )
        finally:
            if owns_session:
                database.close()

    def complete(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        *,
        operation: str = "chat",
        session_id: str | None = None,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        db_session: Session | None = None,
    ) -> str:
        """Synchronous completion with telemetry."""

        resolved_model = self._model_for_provider(self.provider, model)
        started = perf_counter()
        if self.provider == "siliconflow":
            result = self._siliconflow_complete(
                messages, resolved_model, max_tokens, temperature
            )
        elif self.provider == "anthropic":
            result = self._anthropic_complete(
                messages, resolved_model, max_tokens, temperature
            )
        elif self.provider == "google":
            result = self._google_complete(messages, resolved_model, max_tokens, temperature)
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

        self._persist_usage(
            response=result.response,
            model=resolved_model,
            latency_ms=round((perf_counter() - started) * 1000),
            operation=operation,
            session_id=session_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            db_session=db_session,
        )
        return result.text

    async def complete_async(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        *,
        operation: str = "chat",
        session_id: str | None = None,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        db_session: Session | None = None,
    ) -> str:
        """Asynchronous completion with telemetry."""

        resolved_model = self._model_for_provider(self.provider, model)
        started = perf_counter()
        if self.provider == "siliconflow":
            result = await self._siliconflow_complete_async(
                messages, resolved_model, max_tokens, temperature
            )
        elif self.provider == "anthropic":
            result = await self._anthropic_complete_async(
                messages, resolved_model, max_tokens, temperature
            )
        elif self.provider == "google":
            result = await self._google_complete_async(
                messages, resolved_model, max_tokens, temperature
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

        self._persist_usage(
            response=result.response,
            model=resolved_model,
            latency_ms=round((perf_counter() - started) * 1000),
            operation=operation,
            session_id=session_id,
            task_id=task_id,
            agent_run_id=agent_run_id,
            db_session=db_session,
        )
        return result.text

    async def stream_async(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        *,
        operation: str = "chat",
        session_id: str | None = None,
        task_id: str | None = None,
        agent_run_id: str | None = None,
        db_session: Session | None = None,
    ) -> AsyncIterator[str]:
        """Asynchronous streaming completion with final-response telemetry."""

        resolved_model = self._model_for_provider(self.provider, model)
        telemetry = {
            "operation": operation,
            "session_id": session_id,
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "db_session": db_session,
        }
        if self.provider == "siliconflow":
            stream = self._siliconflow_stream_async(
                messages, resolved_model, max_tokens, temperature, telemetry
            )
        elif self.provider == "anthropic":
            stream = self._anthropic_stream_async(
                messages, resolved_model, max_tokens, temperature, telemetry
            )
        elif self.provider == "google":
            stream = self._google_stream_async(
                messages, resolved_model, max_tokens, temperature, telemetry
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {self.provider}")

        async for chunk in stream:
            yield chunk

    @staticmethod
    def _extract_kimi_content(message: dict) -> str:
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")
        return content if content else reasoning

    def _siliconflow_complete(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        with self._get_siliconflow_client() as client:
            response = client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return _ProviderResult(
                self._extract_kimi_content(payload["choices"][0]["message"]),
                payload,
            )

    async def _siliconflow_complete_async(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        async with self._get_async_siliconflow_client() as client:
            response = await client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            response.raise_for_status()
            payload = response.json()
            return _ProviderResult(
                self._extract_kimi_content(payload["choices"][0]["message"]),
                payload,
            )

    async def _siliconflow_stream_async(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        telemetry: dict[str, Any],
    ) -> AsyncIterator[str]:
        started = perf_counter()
        usage_payload: Any = {}
        request_succeeded = False
        try:
            async with self._get_async_siliconflow_client() as client:
                async with client.stream(
                    "POST",
                    "/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                ) as response:
                    response.raise_for_status()
                    request_succeeded = True
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("usage") is not None:
                            usage_payload = chunk["usage"]
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content") or delta.get("reasoning_content")
                        if content:
                            yield content
        finally:
            if request_succeeded:
                self._persist_usage(
                    response={"usage": usage_payload},
                    model=model,
                    latency_ms=round((perf_counter() - started) * 1000),
                    **telemetry,
                )

    @staticmethod
    def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
        system_parts: list[str] = []
        provider_messages: list[dict] = []
        for message in messages:
            if message["role"] == "system":
                system_parts.append(str(message["content"]))
            else:
                provider_messages.append(message)
        return "\n\n".join(system_parts), provider_messages

    def _anthropic_complete(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        import anthropic

        system, provider_messages = self._anthropic_messages(messages)
        with anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) as client:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=provider_messages,
            )
        return _ProviderResult(response.content[0].text, response)

    async def _anthropic_complete_async(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        import anthropic

        system, provider_messages = self._anthropic_messages(messages)
        async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=provider_messages,
            )
        return _ProviderResult(response.content[0].text, response)

    async def _anthropic_stream_async(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        telemetry: dict[str, Any],
    ) -> AsyncIterator[str]:
        import anthropic

        system, provider_messages = self._anthropic_messages(messages)
        started = perf_counter()
        final_response: Any = None
        request_succeeded = False
        try:
            async with anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY) as client:
                async with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    messages=provider_messages,
                ) as stream:
                    request_succeeded = True
                    async for text in stream.text_stream:
                        yield text
                    final_response = await stream.get_final_message()
        finally:
            if request_succeeded:
                self._persist_usage(
                    response=final_response or {},
                    model=model,
                    latency_ms=round((perf_counter() - started) * 1000),
                    **telemetry,
                )

    @staticmethod
    def _google_request(messages: list[dict]) -> tuple[list[dict], str]:
        system_parts: list[str] = []
        contents: list[dict] = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system":
                system_parts.append(str(message.get("content", "")))
                continue
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": str(message.get("content", ""))}],
                }
            )
        return contents, "\n\n".join(system_parts)

    def _google_complete(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        contents, system = self._google_request(messages)
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system
        client = self._get_google_client()
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return _ProviderResult(response.text or "", response)
        finally:
            client.close()

    async def _google_complete_async(
        self, messages: list[dict], model: str, max_tokens: int, temperature: float
    ) -> _ProviderResult:
        contents, system = self._google_request(messages)
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system
        client = self._get_google_client()
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return _ProviderResult(response.text or "", response)
        finally:
            await client.aio.aclose()
            client.close()

    async def _google_stream_async(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int,
        temperature: float,
        telemetry: dict[str, Any],
    ) -> AsyncIterator[str]:
        contents, system = self._google_request(messages)
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system
        client = self._get_google_client()
        started = perf_counter()
        final_response: Any = None
        request_succeeded = False
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            request_succeeded = True
            async for chunk in stream:
                final_response = chunk
                if chunk.text:
                    yield chunk.text
        finally:
            try:
                await client.aio.aclose()
            finally:
                client.close()
            if request_succeeded:
                self._persist_usage(
                    response=final_response or {},
                    model=model,
                    latency_ms=round((perf_counter() - started) * 1000),
                    **telemetry,
                )


llm = LLMClient()
