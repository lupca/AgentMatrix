from __future__ import annotations

from app.services.tool_specs.base import DEFERRED_GROUPS, ToolSpec

SESSION_TOOL_SPECS: list[ToolSpec] = [
            ToolSpec(
                name="compact_context",
                description=(
                    "Use this on your own session when it has grown long and older "
                    "messages are eating context budget you'd rather spend on the "
                    "current work -- it summarizes older session messages in place. "
                    "This is not save_project_context, which persists durable "
                    "project conventions for future sessions to read; "
                    "compact_context only shrinks this session's own history. No "
                    "precondition beyond having an active session. There's no "
                    "specific rejection path -- if it doesn't help enough, keep "
                    "working and call it again later as the session grows further."
                ),
                parameters={"type": "object", "properties": {}},
                handler="compact_context",
                tier="deferred",
                permission="write",
                entity="sessions",
                slash_alias="/compact",
                group="session",
            ),
            ToolSpec(
                name="load_tools",
                description=(
                    "Use this when you want to call a deferred tool that isn't in "
                    "your baseline schema set yet -- call load_tools with its group "
                    "first, in the same turn, before invoking that tool. This is "
                    "not a normal working tool: it doesn't touch tasks or data, "
                    "just exposes more schemas for the rest of this turn. Only "
                    "relevant to the OpenAI-style tool loop (the eager/deferred "
                    "split doesn't apply over MCP, where every tool is already "
                    "exposed). Groups: task_lifecycle (dispatch_task, "
                    "record_verdict, approve_gate, cancel_task, request_review, "
                    "update_task, archive_task, generate_spec_plan), admin "
                    "(project/agent/knowledge/settings management), session "
                    "(compact_context), research (get_minimal_context, "
                    "get_impact_radius), query (get_task_events, suggest_agents), "
                    "spec (impl_design, spec_write, spec_get, spec_stale). If a "
                    "tool call still fails as unrecognized after loading its "
                    "group, double check the group name against this list and "
                    "load_tools again with the right one."
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
