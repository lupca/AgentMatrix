from __future__ import annotations

from app.services.tool_specs.base import DEFERRED_GROUPS, ToolSpec

ADMIN_TOOL_SPECS: list[ToolSpec] = [
            ToolSpec(
                name="manage_project",
                description=(
                    "Use this when the unit you're creating/changing is the "
                    "project itself -- its repo_root, name, mode, status -- rather "
                    "than a task inside it; action=create/update/archive/restore, "
                    "with no hard delete. This is not update_task, which edits a "
                    "task's own fields and never touches project-level settings. "
                    "Admin-permission: in supervised mode this creates a pending "
                    "gate awaiting approve_gate rather than applying immediately; "
                    "in bypass mode it applies right away. If the call returns a "
                    "pending admin gate, call approve_gate with the 'admin:<id>' "
                    "form to let it proceed."
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
                    "Use this to register a new CLI/API agent, change one's "
                    "roles/capabilities/model/effort, or disable/archive/restore "
                    "one -- action=create/update/disable/archive/restore. api_key "
                    "is write-only: it is encrypted before any record is "
                    "persisted, never echoed back, and never readable through any "
                    "tool -- check has_api_key instead of expecting api_key back. "
                    "This is not suggest_agents, which only ranks existing agents "
                    "for a task and never mutates the roster. Admin-permission: in "
                    "supervised mode this creates a pending gate awaiting "
                    "approve_gate rather than applying immediately; in bypass mode "
                    "it applies right away. If it returns a pending admin gate, "
                    "call approve_gate with the 'admin:<id>' form."
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
                        "role": {
                            "type": "string",
                            "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                            "description": "Primary role (legacy). Prefer 'roles' for multi-role agents.",
                        },
                        "roles": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["executor", "reviewer", "coordinator", "spec_plan"],
                            },
                            "description": "Agent roles. Most agents have [executor, reviewer].",
                        },
                        "status": {"type": "string"},
                        "capabilities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Agent capabilities (code, backend, review, architecture, etc.)",
                        },
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
                description=(
                    "Use this to save or edit a standalone knowledge-base article -- "
                    "action=create/update/archive/restore, no hard delete. This is "
                    "not manage_notes: manage_notes handles smaller agent notes with "
                    "many-to-many links to specific projects/tasks and semantic "
                    "search; manage_knowledge is for titled, categorized reference "
                    "content with no gate to approve. No status precondition beyond "
                    "an id existing for update/archive/restore. If update/archive "
                    "is rejected for an unknown id, list existing items via "
                    "query_db (entity knowledge_items) to find the right one."
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
                    "Use this to change a system-wide setting like autonomy mode -- "
                    "not a specific project's or agent's own fields (use "
                    "manage_project/manage_agent for those). Reads the whitelist "
                    "via query_db (SELECT key, value FROM settings) to see what's "
                    "writable; keys outside the whitelist are rejected. "
                    "Admin-permission: in supervised mode this creates a pending "
                    "gate awaiting approve_gate rather than applying immediately; "
                    "in bypass mode it applies right away -- if it returns a "
                    "pending admin gate, call approve_gate with the 'admin:<id>' "
                    "form. "
                    "The `autonomy` setting controls task mode behavior: "
                    "`supervised` requires human approval at every gate, "
                    "`auto` allows low-risk tasks to bypass gates automatically, "
                    "and `plan-only` blocks dispatch entirely. "
                    "The `default_mode` key is not a writable setting and will "
                    "be rejected."
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
]
