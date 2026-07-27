"""Anthropic tool schemas for coordinator slash commands.

This is now a thin projection over ``app.services.tool_registry`` (the single
source of truth, ADR-001 §D1). Rarely-used tools are marked
``defer_loading`` so their schemas stay out of context until the model
searches for them (Anthropic tool search), which is where most of the ~10%
context saving comes from.
"""

from __future__ import annotations

from typing import Any

from app.services.tool_registry import TOOL_REGISTRY, to_openai_tools

# Required alongside any deferred tool so the model can discover it on demand.
# Must itself never be marked defer_loading.
TOOL_SEARCH_TOOL: dict[str, Any] = {
    "type": "tool_search_tool_regex_20251119",
    "name": "tool_search_tool_regex",
}


def get_tool_definitions(*, include_deferred: bool = True) -> list[dict[str, Any]]:
    """Return the tool schema list for the coordinator's global context.

    Eager tools are always included in full. When ``include_deferred`` is
    true, the deferred tools are appended with ``defer_loading: True`` plus
    the tool-search tool required to discover them.
    """

    eager = [spec for spec in TOOL_REGISTRY.values() if spec.tier == "eager"]
    deferred = [spec for spec in TOOL_REGISTRY.values() if spec.tier == "deferred"]

    tools = to_openai_tools(eager)
    if include_deferred and deferred:
        tools.append(dict(TOOL_SEARCH_TOOL))
        tools.extend({**tool, "defer_loading": True} for tool in to_openai_tools(deferred))
    return tools
