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


# Deferred-tool groups loadable via the ``load_tools`` meta-tool (ADR-001
# §D3). "admin" has no members yet (Phase 2c) but is listed so the schema
# enum and system-prompt hint stay accurate ahead of that work.
DEFERRED_GROUPS: tuple[str, ...] = ("task_lifecycle", "admin", "session", "research")


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
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Task ids that must reach 'done' before this task "
                            "may dispatch"
                        ),
                    },
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
                            "settings",
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
                    "effort": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "extra-high", "max"],
                        "description": (
                            "Override the executor agent's default effort for "
                            "this dispatch only."
                        ),
                    },
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
            name="request_review",
            description=(
                "Move a task from awaiting-review to in-review by dispatching "
                "a real /code-review run against its committed base..head "
                "range. If reviewer is omitted, one is auto-selected and is "
                "always independent from the executor (four-eyes); if no "
                "independent reviewer is available this fails rather than "
                "lowering the bar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "reviewer": {
                        "type": "string",
                        "description": "Agent id to review; auto-selected if omitted.",
                    },
                },
                "required": ["task_id"],
            },
            handler="request_review",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/request-review",
            group="task_lifecycle",
        ),
        ToolSpec(
            name="generate_spec_plan",
            description=(
                "Run the spec/plan gate for a 'todo' task: one LLM call, "
                "grounded by research-tool graph queries, produces "
                "acceptance_criteria/plan/files/tests/risk and writes them "
                "onto the task. A task cannot be dispatched until this has "
                "run (or the task is exempted via legacy_no_ac)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Agent to generate the spec/plan; auto-suggested "
                            "if omitted."
                        ),
                    },
                },
                "required": ["task_id"],
            },
            handler="generate_spec_plan",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias="/spec-plan",
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
        ToolSpec(
            name="manage_project",
            description=(
                "Create, update, or archive a project. No hard delete — "
                "archive sets status to 'archived'. Admin-permission: in "
                "supervised mode this creates a pending gate awaiting "
                "/approve; in bypass mode it applies immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Project id (required for update/archive).",
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "context_md": {"type": "string"},
                    "status": {"type": "string"},
                    "repo_root": {"type": "string"},
                    "task_prefix": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["action"],
            },
            handler="manage_project",
            tier="deferred",
            permission="admin",
            entity="projects",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="manage_agent",
            description=(
                "Create, update, or disable an agent. Rejects any payload "
                "containing an api_key — configure API-agent credentials "
                "through the REST API instead; use has_api_key to check "
                "whether one is set. Admin-permission: in supervised mode "
                "this creates a pending gate awaiting /approve; in bypass "
                "mode it applies immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "disable"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Agent id (required for update/disable).",
                    },
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "status": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                    "effort": {"type": "string"},
                    "cli": {"type": "string"},
                    "agent_type": {"type": "string", "enum": ["cli", "api"]},
                    "provider": {
                        "type": "string",
                        "enum": ["anthropic", "google", "openai"],
                    },
                    "base_url": {"type": "string"},
                    "is_default": {"type": "boolean"},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["action"],
            },
            handler="manage_agent",
            tier="deferred",
            permission="admin",
            entity="agents",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="manage_knowledge",
            description="Create, update, or archive a knowledge item. No hard delete.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive"],
                    },
                    "id": {
                        "type": "string",
                        "description": "Knowledge item id (required for update/archive).",
                    },
                    "title": {"type": "string"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "project": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["action"],
            },
            handler="manage_knowledge",
            tier="deferred",
            permission="write",
            entity="knowledge",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="update_settings",
            description=(
                "Update a whitelisted system setting (see query_db "
                "entity=settings for readable keys). Admin-permission: in "
                "supervised mode this creates a pending gate awaiting "
                "/approve; in bypass mode it applies immediately. Rejects "
                "keys outside the whitelist."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Setting key; must be on the whitelist.",
                    },
                    "value": {"description": "New value for the setting (any JSON type)."},
                    "mode": {
                        "type": "string",
                        "enum": ["supervised", "bypass"],
                        "default": "supervised",
                        "description": "Gate mode for this mutation.",
                    },
                },
                "required": ["key", "value"],
            },
            handler="update_settings",
            tier="deferred",
            permission="admin",
            entity="settings",
            slash_alias=None,
            group="admin",
        ),
        ToolSpec(
            name="update_task",
            description=(
                "Edit a task's plan, acceptance criteria, priority, or tags. "
                "Does not change task status — use dispatch_task, "
                "record_verdict, or approve_gate for status transitions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "patch": {
                        "type": "object",
                        "description": (
                            "Fields to update: plan, acceptance_criteria, "
                            "priority, tags."
                        ),
                    },
                },
                "required": ["task_id", "patch"],
            },
            handler="update_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias=None,
            group="task_lifecycle",
        ),
        ToolSpec(
            name="get_minimal_context",
            description=(
                "Read-only semantic search over the project's code graph and "
                "return compact context relevant to a query."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Code or task context to find."},
                    "limit": {"type": "integer", "default": 10, "description": "Maximum matching nodes."},
                },
                "required": ["query"],
            },
            handler="get_minimal_context",
            tier="deferred",
            permission="read",
            entity="research",
            slash_alias=None,
            group="research",
        ),
        ToolSpec(
            name="get_impact_radius",
            description="Read-only list of files affected by changing a project file.",
            parameters={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Project-relative file path."},
                },
                "required": ["file"],
            },
            handler="get_impact_radius",
            tier="deferred",
            permission="read",
            entity="research",
            slash_alias=None,
            group="research",
        ),
        ToolSpec(
            name="load_tools",
            description=(
                "Load additional tool schemas for the rest of this turn. Call "
                "this before using a tool that isn't in the baseline set. "
                "Groups: task_lifecycle (dispatch_task, record_verdict, "
                "approve_gate, cancel_task, request_review), admin (project/agent/knowledge/"
                "settings management), session (compact_context), research "
                "(get_minimal_context, get_impact_radius)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "enum": list(DEFERRED_GROUPS),
                        "description": "Tool group to load.",
                    },
                },
                "required": ["group"],
            },
            handler="load_tools",
            tier="eager",
            permission="read",
            entity="meta",
            slash_alias=None,
            group="meta",
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
