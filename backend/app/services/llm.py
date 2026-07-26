"""
LLM abstraction layer - supports multiple providers.
Default: SiliconFlow (Kimi-K3)
Fallback: Anthropic (Claude)
"""
import os
import httpx
from typing import AsyncIterator

# Provider config
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "siliconflow")  # siliconflow | anthropic

# SiliconFlow config
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY")
SILICONFLOW_BASE_URL = "https://api.siliconflow.com/v1"
SILICONFLOW_MODEL = os.getenv("SILICONFLOW_MODEL", "moonshotai/Kimi-K3")

# Anthropic config (fallback)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


class LLMClient:
    """Unified LLM client supporting multiple providers."""

    def __init__(self, provider: str = None):
        self.provider = provider or LLM_PROVIDER

    def _get_siliconflow_client(self):
        return httpx.Client(
            base_url=SILICONFLOW_BASE_URL,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json"
            },
            timeout=60.0
        )

    def _get_async_siliconflow_client(self):
        return httpx.AsyncClient(
            base_url=SILICONFLOW_BASE_URL,
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json"
            },
            timeout=60.0
        )

    def complete(
        self,
        messages: list[dict],
        model: str = None,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> str:
        """Synchronous completion."""
        if self.provider == "siliconflow":
            return self._siliconflow_complete(messages, model, max_tokens, temperature)
        else:
            return self._anthropic_complete(messages, model, max_tokens, temperature)

    async def complete_async(
        self,
        messages: list[dict],
        model: str = None,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> str:
        """Async completion."""
        if self.provider == "siliconflow":
            return await self._siliconflow_complete_async(messages, model, max_tokens, temperature)
        else:
            return await self._anthropic_complete_async(messages, model, max_tokens, temperature)

    async def stream_async(
        self,
        messages: list[dict],
        model: str = None,
        max_tokens: int = 2048,
        temperature: float = 0.7
    ) -> AsyncIterator[str]:
        """Async streaming completion."""
        if self.provider == "siliconflow":
            async for chunk in self._siliconflow_stream_async(messages, model, max_tokens, temperature):
                yield chunk
        else:
            async for chunk in self._anthropic_stream_async(messages, model, max_tokens, temperature):
                yield chunk

    # ===== SiliconFlow Implementation =====

    def _extract_kimi_content(self, message: dict) -> str:
        """Extract content from Kimi-K3 response (handles reasoning_content fallback)."""
        content = message.get("content", "")
        reasoning = message.get("reasoning_content", "")
        # Kimi-K3 may put response in reasoning_content if content is empty
        return content if content else reasoning

    def _siliconflow_complete(self, messages, model, max_tokens, temperature):
        model = model or SILICONFLOW_MODEL
        with self._get_siliconflow_client() as client:
            response = client.post("/chat/completions", json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature
            })
            response.raise_for_status()
            msg = response.json()["choices"][0]["message"]
            return self._extract_kimi_content(msg)

    async def _siliconflow_complete_async(self, messages, model, max_tokens, temperature):
        model = model or SILICONFLOW_MODEL
        async with self._get_async_siliconflow_client() as client:
            response = await client.post("/chat/completions", json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature
            })
            response.raise_for_status()
            msg = response.json()["choices"][0]["message"]
            return self._extract_kimi_content(msg)

    async def _siliconflow_stream_async(self, messages, model, max_tokens, temperature):
        model = model or SILICONFLOW_MODEL
        async with self._get_async_siliconflow_client() as client:
            async with client.stream("POST", "/chat/completions", json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True
            }) as response:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        import json
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta", {})
                        # Handle Kimi-K3 reasoning_content in streaming
                        content = delta.get("content") or delta.get("reasoning_content")
                        if content:
                            yield content

    # ===== Anthropic Implementation (fallback) =====

    def _anthropic_complete(self, messages, model, max_tokens, temperature):
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # Convert to Anthropic format
        system = None
        anthro_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthro_messages.append(m)

        response = client.messages.create(
            model=model or "claude-3-5-sonnet-latest",
            max_tokens=max_tokens,
            system=system or "",
            messages=anthro_messages
        )
        return response.content[0].text

    async def _anthropic_complete_async(self, messages, model, max_tokens, temperature):
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        system = None
        anthro_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthro_messages.append(m)

        response = await client.messages.create(
            model=model or "claude-3-5-sonnet-latest",
            max_tokens=max_tokens,
            system=system or "",
            messages=anthro_messages
        )
        return response.content[0].text

    async def _anthropic_stream_async(self, messages, model, max_tokens, temperature):
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

        system = None
        anthro_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthro_messages.append(m)

        async with client.messages.stream(
            model=model or "claude-3-5-sonnet-latest",
            max_tokens=max_tokens,
            system=system or "",
            messages=anthro_messages
        ) as stream:
            async for text in stream.text_stream:
                yield text


# Default client
llm = LLMClient()
