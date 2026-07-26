"""Anthropic tool schemas for coordinator slash commands.

These mirror the commands handled by ``CommandRouter`` (see
``app/services/command_router.py``) so the model can see them as callable
tools rather than only as free-text slash syntax. Rarely-used commands are
marked ``defer_loading`` so their schemas stay out of context until the model
searches for them (Anthropic tool search), which is where most of the ~10%
context saving comes from.
"""

from __future__ import annotations

from typing import Any

# Frequently used in a chat session — loaded eagerly (defer_loading=False).
EAGER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "pm_create_task",
        "description": (
            "Create a new task. Use when the user asks to create/add/start a "
            "new task or work item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title"},
                "project": {"type": "string", "description": "Project id"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "get_status",
        "description": "Get the status of a task, or list recent tasks if no id is given.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id, optional"},
            },
        },
    },
]

# Rarely used mid-conversation — deferred so their schemas don't consume
# context until the model looks them up via tool search.
DEFERRED_TOOLS: list[dict[str, Any]] = [
    {
        "name": "dispatch_task",
        "description": "Assign an executor to a task and move it to dispatched status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "executor": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "record_verdict",
        "description": "Record a review verdict (pass/changes) for a dispatched task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "changes"]},
                "findings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "verdict"],
        },
    },
    {
        "name": "approve_gate",
        "description": "Approve a gate awaiting human confirmation before it proceeds.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "cancel_task",
        "description": "Cancel a task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "compact_context",
        "description": "Summarize older session messages to reduce context size.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

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

    tools = [dict(tool) for tool in EAGER_TOOLS]
    if include_deferred and DEFERRED_TOOLS:
        tools.append(dict(TOOL_SEARCH_TOOL))
        tools.extend({**tool, "defer_loading": True} for tool in DEFERRED_TOOLS)
    return tools
