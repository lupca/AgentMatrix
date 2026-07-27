"""OpenAI tool schemas for the coordinator's baseline (eager) tool set.

Deferred tools are no longer sent eagerly nor discovered via Anthropic-only
tool search (``defer_loading`` / ``tool_search_tool_regex`` are a no-op on
OpenAI). Instead the model calls the ``load_tools`` meta-tool to pull a
group's schemas into the tool-execution loop for the rest of the turn
(ADR-001 §D3, ``CoordinatorService``).
"""

from __future__ import annotations

from typing import Any

from app.services.tool_registry import TOOL_REGISTRY, to_openai_tools


def get_tool_definitions() -> list[dict[str, Any]]:
    """Baseline tool schemas sent on every request: the eager tools."""

    eager = [spec for spec in TOOL_REGISTRY.values() if spec.tier == "eager"]
    return to_openai_tools(eager)
