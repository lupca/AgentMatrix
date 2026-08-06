from __future__ import annotations

from app.services.tool_specs.admin_specs import ADMIN_TOOL_SPECS
from app.services.tool_specs.base import (
    DEFERRED_GROUPS,
    Permission,
    Role,
    Tier,
    ToolSpec,
)
from app.services.tool_specs.query_specs import QUERY_TOOL_SPECS
from app.services.tool_specs.research_specs import RESEARCH_TOOL_SPECS
from app.services.tool_specs.session_specs import SESSION_TOOL_SPECS
from app.services.tool_specs.spec_specs import SPEC_TOOL_SPECS
from app.services.tool_specs.task_specs import TASK_TOOL_SPECS

# Collect all specs from sub-modules
_unmerged_specs = {
    spec.name: spec
    for spec in [
        *TASK_TOOL_SPECS,
        *QUERY_TOOL_SPECS,
        *ADMIN_TOOL_SPECS,
        *SESSION_TOOL_SPECS,
        *RESEARCH_TOOL_SPECS,
        *SPEC_TOOL_SPECS,
    ]
}

# Original canonical declaration order preserved for dict key iteration
_CANONICAL_ORDER = [
    "create_task",
    "get_status",
    "manage_inbox",
    "ask_human",
    "get_run_output",
    "get_stats",
    "query_db",
    "dispatch_task",
    "record_verdict",
    "attach_result",
    "approve_gate",
    "land_task",
    "cancel_task",
    "reopen_task",
    "get_task_events",
    "wait_for_task",
    "archive_task",
    "suggest_agents",
    "request_review",
    "generate_spec_plan",
    "critique_spec_plan",
    "compact_context",
    "manage_project",
    "manage_agent",
    "manage_knowledge",
    "manage_notes",
    "update_settings",
    "update_task",
    "get_minimal_context",
    "get_impact_radius",
    "save_project_context",
    "impl_design",
    "spec_write",
    "spec_get",
    "spec_stale",
    "load_tools",
]

ALL_TOOL_SPECS: dict[str, ToolSpec] = {
    name: _unmerged_specs[name]
    for name in _CANONICAL_ORDER
    if name in _unmerged_specs
}

__all__ = [
    "ToolSpec",
    "Tier",
    "Permission",
    "Role",
    "DEFERRED_GROUPS",
    "ALL_TOOL_SPECS",
    "TASK_TOOL_SPECS",
    "QUERY_TOOL_SPECS",
    "ADMIN_TOOL_SPECS",
    "SESSION_TOOL_SPECS",
    "RESEARCH_TOOL_SPECS",
    "SPEC_TOOL_SPECS",
]
