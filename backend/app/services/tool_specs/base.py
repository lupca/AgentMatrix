from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Tier = Literal["eager", "deferred"]
Permission = Literal["read", "write", "admin"]
Role = Literal["coordinator", "executor"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    """Canonical tool name, e.g. create_task."""

    description: str
    parameters: dict[str, Any]
    """JSON Schema for the tool's arguments (OpenAI function format)."""

    handler: str
    """Command identifier CommandRouter.execute dispatches to (_handle_<handler>)."""

    tier: Tier
    permission: Permission
    entity: str
    slash_alias: str | None
    group: str
    required_role: Role = "coordinator"
    infer_task_scope: bool = True
    """Whether an executor token fills an omitted optional task_id."""


DEFERRED_GROUPS: tuple[str, ...] = (
    "task_lifecycle",
    "admin",
    "session",
    "research",
    "query",
    "spec",
)
