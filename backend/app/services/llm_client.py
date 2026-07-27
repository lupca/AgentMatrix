"""Shared model constants and telemetry helpers.

Model invocation belongs to :mod:`app.services.llm_service`; this module is
kept as the stable home for usage normalization and pricing utilities.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

logger = logging.getLogger(__name__)

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "siliconflow").lower()
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1")
SILICONFLOW_MODEL = os.getenv("SILICONFLOW_MODEL", "moonshotai/Kimi-K3")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
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


# Fallback pricing when DB not available
PROVIDER_FALLBACK_PRICING: dict[str, ModelPricing] = {
    "anthropic": ModelPricing(Decimal("3.00"), Decimal("15.00"), Decimal("0.30")),
    "google": ModelPricing(Decimal("0.30"), Decimal("2.50"), Decimal("0.03")),
    "siliconflow": ModelPricing(Decimal("0.60"), Decimal("2.50"), Decimal("0.60")),
    "openai": ModelPricing(Decimal("1.00"), Decimal("4.00"), Decimal("0.50")),
}

# Cache for DB pricing (refreshed on each call for simplicity)
_pricing_cache: dict[str, ModelPricing] = {}
_cache_loaded: bool = False


def _load_pricing_from_db() -> dict[str, ModelPricing]:
    """Load pricing from database model_pricing table."""
    global _pricing_cache, _cache_loaded
    if _cache_loaded:
        return _pricing_cache

    try:
        from app.db.base import SessionLocal
        from app.db.models import ModelPricing as ModelPricingDB

        db = SessionLocal()
        try:
            rows = db.query(ModelPricingDB).all()
            for row in rows:
                key = row.model.lower()
                _pricing_cache[key] = ModelPricing(
                    input=Decimal(str(row.input_price_per_mtok or 1)),
                    output=Decimal(str(row.output_price_per_mtok or 4)),
                    cached_input=Decimal(str(row.cached_input_price_per_mtok or 0.5)),
                )
            _cache_loaded = True
            logger.debug("Loaded %d pricing entries from database", len(_pricing_cache))
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to load pricing from DB, using fallbacks: %s", e)

    return _pricing_cache


def refresh_pricing_cache() -> None:
    """Force refresh pricing cache from database."""
    global _pricing_cache, _cache_loaded
    _pricing_cache = {}
    _cache_loaded = False
    _load_pricing_from_db()


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
    """Extract and normalize usage from common provider response shapes."""

    provider = provider.lower()
    if provider == "google":
        usage = _read_value(response, "usage_metadata", "usageMetadata", default=None) or response
        cached = _nonnegative_int(_read_value(usage, "cached_content_token_count", "cachedContentTokenCount"))
        prompt = _nonnegative_int(_read_value(usage, "prompt_token_count", "promptTokenCount", "input_tokens"))
        candidates = _nonnegative_int(_read_value(usage, "candidates_token_count", "candidatesTokenCount", "output_tokens"))
        thoughts = _nonnegative_int(_read_value(usage, "thoughts_token_count", "thoughtsTokenCount"))
        return UsageCounts(max(prompt, cached), candidates + thoughts, min(cached, max(prompt, cached)))

    usage = _read_value(response, "usage", default=None) or response
    if provider == "anthropic":
        uncached = _nonnegative_int(_read_value(usage, "input_tokens", "inputTokens"))
        cache_read = _nonnegative_int(_read_value(usage, "cache_read_input_tokens", "cacheReadInputTokens"))
        cache_creation = _nonnegative_int(_read_value(usage, "cache_creation_input_tokens", "cacheCreationInputTokens"))
        return UsageCounts(
            uncached + cache_read + cache_creation,
            _nonnegative_int(_read_value(usage, "output_tokens", "outputTokens")),
            cache_read,
        )

    prompt = _nonnegative_int(_read_value(usage, "prompt_tokens", "promptTokens", "input_tokens", "inputTokens"))
    output = _nonnegative_int(_read_value(usage, "completion_tokens", "completionTokens", "output_tokens", "outputTokens"))
    details = _read_value(usage, "prompt_tokens_details", "promptTokensDetails", default=None)
    cached = _nonnegative_int(_read_value(details, "cached_tokens", "cachedTokens"))
    return UsageCounts(prompt, output, min(cached, prompt))


def get_model_pricing(model: str, provider: str) -> ModelPricing:
    """Get pricing from DB cache, fallback to provider defaults."""
    normalized = (model or "").lower()

    # Try DB cache first
    db_pricing = _load_pricing_from_db()
    if db_pricing:
        # Exact match
        if normalized in db_pricing:
            return db_pricing[normalized]
        # Partial match (longest key first)
        for model_key in sorted(db_pricing, key=len, reverse=True):
            if model_key in normalized:
                return db_pricing[model_key]

    # Fallback to provider defaults
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


__all__ = [
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "GOOGLE_API_KEY", "GOOGLE_MODEL",
    "LLM_PROVIDER", "ModelPricing", "OPENAI_MODEL", "SILICONFLOW_API_KEY",
    "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL", "UsageCounts", "calculate_cost",
    "extract_usage", "get_model_pricing", "refresh_pricing_cache",
]
