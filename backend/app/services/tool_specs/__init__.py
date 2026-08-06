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

# The order `TOOL_REGISTRY` had before the split (CTV2-1417), kept so dict
# iteration -- and every projection that walks it -- stays byte-identical.
#
# It is an ORDERING hint, never a membership list. The first draft built the
# registry as `{name: specs[name] for name in _CANONICAL_ORDER if name in specs}`,
# which silently DROPPED any tool declared in a `*_specs.py` file but forgotten
# here: no error, no warning, the tool just stopped existing on the MCP surface.
# Reviewer @claude-sonnet-high caught it on the CTV2-1417 diff while the two
# lists still happened to match 36/36, so nothing had broken yet.
#
# `_ordered_specs` below cannot lose a tool: unknown names sort to the end
# instead of vanishing. Silent failure is this system's most expensive bug
# family -- an ordering nicety must never be able to cause one.
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

def _ordered_specs() -> dict[str, ToolSpec]:
    """Every declared spec, in canonical order, with strays appended.

    Membership comes from what the `*_specs.py` modules actually declare;
    `_CANONICAL_ORDER` only decides where each one sits. A tool missing from
    that list keeps working and lands at the end of the registry.
    """
    rank = {name: index for index, name in enumerate(_CANONICAL_ORDER)}
    return {
        name: _unmerged_specs[name]
        for name in sorted(
            _unmerged_specs, key=lambda name: (rank.get(name, len(rank)), name)
        )
    }


ALL_TOOL_SPECS: dict[str, ToolSpec] = _ordered_specs()

__all__ = [
    "ADMIN_TOOL_SPECS",
    "ALL_TOOL_SPECS",
    "DEFERRED_GROUPS",
    "QUERY_TOOL_SPECS",
    "RESEARCH_TOOL_SPECS",
    "SESSION_TOOL_SPECS",
    "SPEC_TOOL_SPECS",
    "TASK_TOOL_SPECS",
    "Permission",
    "Role",
    "Tier",
    "ToolSpec",
]
