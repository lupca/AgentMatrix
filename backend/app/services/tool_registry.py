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
Role = Literal["coordinator", "executor"]


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
    required_role: Role = "coordinator"


# Deferred-tool groups loadable via the ``load_tools`` meta-tool (ADR-001
# §D3).
DEFERRED_GROUPS: tuple[str, ...] = (
    "task_lifecycle",
    "admin",
    "session",
    "research",
    "query",
)


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
            required_role="executor",
        ),
        ToolSpec(
            name="get_run_output",
            description=(
                "Read persisted output chunks for an executor run. Use this to "
                "inspect progress or a completed run; output is replayable and "
                "does not depend on a live stream."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task scope."},
                    "run_id": {"type": "string", "description": "Agent run id."},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["task_id", "run_id"],
            },
            handler="get_run_output",
            tier="eager",
            permission="read",
            entity="agent_runs",
            slash_alias=None,
            group="query",
            required_role="executor",
        ),
        ToolSpec(
            name="get_stats",
            description=(
                "Return server-computed token usage, cost, and run statistics. "
                "Use task_id or agent_id to narrow the report."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
            },
            handler="get_stats",
            tier="eager",
            permission="read",
            entity="usage",
            slash_alias=None,
            group="query",
        ),
        ToolSpec(
            name="query_db",
            description=(
                "Read-only database query using raw SQL (v2). "
                "Only a single SELECT or WITH statement is allowed. "
                "Use this to answer complex analytical questions or explore data. "
                "Do NOT access the DB directly via other means.\n"
                "Schema Summary:\n"
                "- tasks (id, title, status [todo, dispatched, awaiting-review, in-review, done, cancelled, failed], project, executor, reviewer, priority, mode)\n"
                "- projects (id, name, status, repo_root, mode)\n"
                "- agents (id, name, role [coordinator, executor, reviewer, spec_plan], status, agent_type [cli, api], model)\n"
                "- sessions (id, title, status, context_level, project_id, task_id)\n"
                "- agent_runs (id, task_id, agent_id, kind [execute, review], status [queued, running, success, failed, cancelled], attempt)\n"
                "- knowledge_items (id, title, category, project, author, content)\n"
                "- audit_log (id, task_id, action, actor, created_at)\n"
                "- settings (key, value)\n\n"
                "Examples:\n"
                "SELECT project, count(*) FROM tasks WHERE status='dispatched' GROUP BY project\n"
                "SELECT id, status FROM agents WHERE role='executor'"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A single raw SQL SELECT/WITH statement. Result is limited to 500 rows.",
                    },
                    "entity": {
                        "type": "string",
                        "description": "[DEPRECATED] Entity to query.",
                    },
                    "filters": {
                        "type": "object",
                        "description": "[DEPRECATED] Equality filters.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "[DEPRECATED] Max rows to return.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "[DEPRECATED] Row offset.",
                    },
                    "include_archived": {
                        "type": "boolean",
                        "description": "[DEPRECATED] Include archived rows.",
                    },
                },
                "required": [],
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
            description=(
                "Approve a gate awaiting human confirmation before it "
                "proceeds. Pass gate_record_id when you have it (use the "
                "'admin:<id>' form returned by manage_* tools for admin "
                "gates); task_id alone resolves that task's pending gate."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "gate_record_id": {
                        "type": "string",
                        "description": (
                            "Gate record id, or 'admin:<id>' for an admin "
                            "gate pending from manage_project/manage_agent/"
                            "manage_knowledge/update_settings."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task whose pending gate to approve (fallback when gate_record_id is unknown).",
                    },
                    "decision": {
                        "type": "string",
                        "enum": ["approved", "rejected"],
                        "description": "Human decision for the gate; defaults to approved.",
                    },
                },
                "required": [],
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
            name="get_task_events",
            description=(
                "Poll task events with a cursor. Use since_id to fetch only "
                "events newer than the last one you saw; kind=decision "
                "returns events that need a coordinator decision."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Filter to one task; omit for all tasks."},
                    "since_id": {"type": "integer", "description": "Return only events with id greater than this."},
                    "kind": {"type": "string", "enum": ["decision", "info"]},
                    "event_types": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": [],
            },
            handler="get_task_events",
            tier="deferred",
            permission="read",
            entity="tasks",
            slash_alias="/events",
            group="query",
            required_role="executor",
        ),
        ToolSpec(
            name="wait_for_task",
            description=(
                "Long-poll one task until something happens: a status change, "
                "a pending gate that needs approval, or a terminal state "
                "(done/failed/cancelled). Blocks server-side up to "
                "timeout_seconds and returns the task snapshot, the latest "
                "run, and new events in one call — use this instead of "
                "polling get_status on a timer. On timeout (changed=false), "
                "call it again with the returned cursor as since_event_id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "since_event_id": {
                        "type": "integer",
                        "description": "Event cursor from the previous call; also returns any event newer than this.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "default": 55,
                        "description": "How long to block server-side (5-120). Keep below your client's tool timeout.",
                    },
                },
                "required": ["task_id"],
            },
            handler="wait_for_task",
            tier="deferred",
            permission="read",
            entity="tasks",
            slash_alias=None,
            group="query",
            required_role="executor",
        ),
        ToolSpec(
            name="archive_task",
            description=(
                "Archive a task (soft delete preserving history), or restore "
                "a previously archived one with restore=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "restore": {"type": "boolean", "default": False},
                },
                "required": ["task_id"],
            },
            handler="archive_task",
            tier="deferred",
            permission="write",
            entity="tasks",
            slash_alias=None,
            group="task_lifecycle",
        ),
        ToolSpec(
            name="suggest_agents",
            description=(
                "Advisory agent ranking for a task without dispatching: "
                "returns the top candidates with score and reason, using the "
                "same matcher dispatch uses (skill/performance/load/cost/"
                "risk). Read-only."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                        "default": "executor",
                    },
                    "top_n": {"type": "integer", "default": 3},
                },
                "required": ["task_id"],
            },
            handler="suggest_agents",
            tier="deferred",
            permission="read",
            entity="agents",
            slash_alias=None,
            group="query",
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
                "Create, update, archive, or restore a project. No hard delete. "
                "Admin-permission: in "
                "supervised mode this creates a pending gate awaiting "
                "/approve; in bypass mode it applies immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive", "restore"],
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
                "Create, update, or disable an agent. api_key is write-only: "
                "it is encrypted before any record is persisted, never "
                "echoed back, and never readable through any tool — use "
                "has_api_key to check whether one is set. Admin-permission: "
                "in supervised mode this creates a pending gate awaiting "
                "/approve; in bypass mode it applies immediately."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "disable", "archive", "restore"],
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
                    "api_key": {
                        "type": "string",
                        "description": (
                            "API credential for agent_type=api. Write-only: "
                            "encrypted at rest, redacted from gate/audit "
                            "records, never returned."
                        ),
                    },
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
            description="Create, update, archive, or restore a knowledge item. No hard delete.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "archive", "restore"],
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
                "Edit a task's plan, acceptance criteria, priority, tags, or "
                "dependencies. Does not change task status — use "
                "dispatch_task, record_verdict, or approve_gate for status "
                "transitions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "patch": {
                        "type": "object",
                        "description": (
                            "Fields to update: plan, acceptance_criteria, "
                            "priority, tags. Dependency edits: "
                            "add_depends_on / remove_depends_on (arrays of "
                            "task ids; cycles are rejected)."
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
            required_role="executor",
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
            required_role="executor",
        ),
        ToolSpec(
            name="save_project_context",
            description=(
                "Save generated project context (context_md) and up to 5 scoped "
                "rules for a project. Used after scanning a repo to persist "
                "conventions/boundaries that get injected into future dispatch "
                "and review prompts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Task id this context-generation run is scoped to "
                            "(required for executor tokens to pass task-scope check)"
                        ),
                    },
                    "project_id": {"type": "string", "description": "Project id"},
                    "context_md": {
                        "type": "string",
                        "description": "Markdown project context, max 150 lines",
                    },
                    "rules": {
                        "type": "array",
                        "description": "Up to 5 scoped rules",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "globs": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "content": {"type": "string"},
                            },
                            "required": ["name", "content"],
                        },
                    },
                },
                "required": ["task_id", "project_id", "context_md"],
            },
            handler="save_project_context",
            tier="deferred",
            permission="write",
            entity="projects",
            slash_alias=None,
            group="task_lifecycle",
            required_role="executor",
        ),
        ToolSpec(
            name="load_tools",
            description=(
                "Load additional tool schemas for the rest of this turn. Call "
                "this before using a tool that isn't in the baseline set. "
                "Groups: task_lifecycle (dispatch_task, record_verdict, "
                "approve_gate, cancel_task, request_review, update_task, "
                "archive_task, generate_spec_plan), admin (project/agent/"
                "knowledge/settings management), session (compact_context), "
                "research (get_minimal_context, get_impact_radius), query "
                "(get_task_events, suggest_agents)."
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
