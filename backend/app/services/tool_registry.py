"""Single source of truth for every coordinator tool (ADR-001 §D1).

Each tool is declared exactly once as a :class:`ToolSpec`. Everything else —
the OpenAI-format schema list (``get_tool_definitions``), the slash-command
table (``CommandRouter``), and the ``GET /api/tools`` dump — is a projection
over ``TOOL_REGISTRY``.
"""

from __future__ import annotations

from typing import Any

from app.services.tool_specs import (
    ALL_TOOL_SPECS,
    DEFERRED_GROUPS,
    Permission,
    Role,
    Tier,
    ToolSpec,
)

# Re-export for backward compatibility and spec anchors
__all__ = [
    "Tier",
    "Permission",
    "Role",
    "ToolSpec",
    "DEFERRED_GROUPS",
    "TOOL_REGISTRY",
    "DEPRECATED_ALIASES",
    "resolve_tool_name",
    "get_spec",
    "get_by_slash_alias",
    "to_openai_tools",
    "get_group_tool_definitions",
    "get_group_for_tool",
    "get_mcp_tool_specs",
    "dump_registry",
]


TOOL_REGISTRY: dict[str, ToolSpec] = ALL_TOOL_SPECS

# Deprecated tool names that must keep routing to a canonical entry so
# sessions started before a rename don't break.
DEPRECATED_ALIASES: dict[str, str] = {
    "pm_create_task": "create_task",
}


def resolve_tool_name(name: str) -> str:
    """Map a (possibly deprecated) tool name to its canonical registry name."""

    return DEPRECATED_ALIASES.get(name, name)


def get_spec(name: str) -> ToolSpec | None:
    """Look up a registry entry by canonical or deprecated tool name."""

    return TOOL_REGISTRY.get(resolve_tool_name(name))


def get_by_slash_alias(alias: str) -> ToolSpec | None:
    for spec in TOOL_REGISTRY.values():
        if spec.slash_alias == alias:
            return spec
    return None


def to_openai_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Project ``ToolSpec`` entries into OpenAI/Anthropic-style function schemas."""

    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.parameters,
        }
        for spec in specs
    ]


def get_group_tool_definitions(group: str) -> list[dict[str, Any]] | None:
    """OpenAI schemas for the deferred tools in ``group`` (ADR-001 §D3).

    Returns ``None`` for a group name outside :data:`DEFERRED_GROUPS`. An
    empty list is a valid result for a group with no tools yet.
    """

    if group not in DEFERRED_GROUPS:
        return None
    deferred = [
        spec
        for spec in TOOL_REGISTRY.values()
        if spec.tier == "deferred" and spec.group == group
    ]
    return to_openai_tools(deferred)


def get_group_for_tool(tool_name: str) -> str | None:
    """Return the deferred group containing ``tool_name``, if any."""

    spec = get_spec(tool_name)
    return spec.group if spec is not None and spec.tier == "deferred" else None


def get_mcp_tool_specs() -> list[ToolSpec]:
    """Tools exposed over the MCP projection for the coordinator chat CLI
    (ADR-001 §D5).

    MCP-connected CLIs manage their own context budget, so the eager/deferred
    split that exists for the OpenAI tool loop doesn't apply here: every
    registry tool is exposed except ``load_tools``, which is a mechanism
    specific to that loop and meaningless over MCP.
    """

    return [spec for spec in TOOL_REGISTRY.values() if spec.name != "load_tools"]


def dump_registry() -> list[dict[str, Any]]:
    """Registry dump for ``GET /api/tools`` (UI tool palette / ``/help``)."""

    return [
        {
            "name": spec.name,
            "description": spec.description,
            "slash_alias": spec.slash_alias,
            "tier": spec.tier,
            "group": spec.group,
        }
        for spec in TOOL_REGISTRY.values()
    ]
