"""Role-aware agent suggestion, reusing `AgentMatcher`'s scoring.

Every caller that needs to pick an agent for a task -- dispatch (executor),
review-order (reviewer), or spec/plan generation (spec_plan) -- goes through
this single service instead of hand-rolling its own capability filter, so the
scoring signal (skill match, historical performance, load, cost) stays
consistent across roles.
"""

from __future__ import annotations

from app.db.models import Task
from app.schemas.agent import AgentSuggestion
from app.services.agent_matcher import AgentMatcher

Role = str

# `None` means "no capability filter" (any agent is eligible); a set means the
# agent's capabilities must intersect it.
_ROLE_CAPABILITIES: dict[str, set[str] | None] = {
    "executor": None,
    "reviewer": None,
    "coordinator": {"coordinator"},
    "spec_plan": {"coordinator", "spec_plan"},
}


class AgentSuggester:
    """Suggest the best agents for a task, filtered by the requested role."""

    def __init__(self, db):
        self.db = db
        self._matcher = AgentMatcher(db)

    def suggest(
        self,
        task: Task,
        *,
        role: str = "executor",
        top_n: int = 3,
    ) -> list[AgentSuggestion]:
        if role not in _ROLE_CAPABILITIES:
            raise ValueError(
                f"Unknown role: {role!r}. Valid roles: "
                f"{', '.join(sorted(_ROLE_CAPABILITIES))}"
            )
        required_capabilities = _ROLE_CAPABILITIES[role]
        return self._matcher.suggest_agents(
            task, top_n=top_n, required_capabilities=required_capabilities
        )
