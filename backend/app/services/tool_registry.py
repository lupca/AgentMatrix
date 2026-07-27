"""Single source of truth for every coordinator tool (ADR-001 §D1).

Each tool is declared exactly once as a :class:`ToolSpec`. Everything else —
the OpenAI-format schema list (``get_tool_definitions``), the slash-command
table (``CommandRouter``), and the ``GET /api/tools`` dump — is a projection
over ``TOOL_REGISTRY``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Tier = Literal["eager", "deferred"]
Permission = Literal["read", "write", "admin"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    """Canonical tool name, e.g. ``create_task``."""

    description: str
    parameters: dict[str, Any]
    """JSON Schema for the tool's arguments (OpenAI function format)."""

    handler: str
    """Command identifier ``CommandRouter.execute`` dispatches to (``_handle_<handler>``)."""

    tier: Tier
    permission: Permission
    entity: str
    slash_alias: str | None
    group: str


TOOL_REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            name="create_task",
            description=(
                "Create a new task. Use when the user asks to create/add/start a "
                "new task or work item."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title"},
                    "project": {"type": "string", "description": "Project id"},
                },
                "required": ["title"],
            },
            handler="create_task",
            tier="eager",
            permission="write",
            entity="tasks",
            slash_alias="/pm",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="get_status",
            description="Get the status of a task, or list recent tasks if no id is given.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id, optional"},
                },
            },
            handler="get_status",
            tier="eager",
            permission="read",
            entity="tasks",
            slash_alias="/status",
            group="query",
        ),
        ToolSpec(
            name="query_db",
            description=(
                "Read-only lookup across Control Tower entities not already "
                "covered by the context snapshot. Returns compact rows "
                "(id/title/status/...), never full rows or secrets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "enum": [
                            "tasks",
                            "projects",
                            "agents",
                            "sessions",
                            "knowledge",
                            "usage",
                        ],
                        "description": "Entity to query.",
                    },
                    "filters": {
                        "type": "object",
                        "description": (
                            "Equality filters; allowed field names vary per "
                            "entity (e.g. tasks: status/project/executor)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return (<=50).",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Row offset for pagination.",
                        "default": 0,
                    },
                },
                "required": ["entity"],
            },
            handler="query_db",
            tier="eager",
            permission="read",
            entity="query",
            slash_alias="/query",
            group="query",
        ),
        ToolSpec(
            name="dispatch_task",
            description="Assign an executor to a task and move it to dispatched status.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "executor": {"type": "string"},
                },
                "required": ["task_id"],
            },
            handler="dispatch_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/dispatch",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="record_verdict",
            description="Record a review verdict (pass/changes) for a dispatched task.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "changes"]},
                    "findings": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["task_id", "verdict"],
            },
            handler="verdict",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/verdict",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="approve_gate",
            description="Approve a gate awaiting human confirmation before it proceeds.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            handler="approve_gate",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/approve",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="cancel_task",
            description="Cancel a task.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            handler="cancel_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/cancel",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="compact_context",
            description="Summarize older session messages to reduce context size.",
            parameters={"type": "object", "properties": {}},
            handler="compact_context",
            tier="deferred",
            permission="write",
            entity="sessions",
            slash_alias="/compact",
            group="session",
        ),
    ]
}

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
