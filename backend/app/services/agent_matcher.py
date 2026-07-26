"""Rank available agents for a task.

The matcher intentionally uses explainable, database-backed signals.  This keeps
suggestions useful before an embeddings/indexing service is available and makes
the score stable enough to show in the dispatch UI.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Agent, AgentRun, Task
from app.schemas.agent import AgentSuggestion


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "to",
    "with", "task", "this", "new", "add", "update", "implement",
}
_ACTIVE_RUN_STATUSES = ("queued", "running")
_UNAVAILABLE_STATUSES = {"offline", "deprecated", "disabled"}


def _tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    result: set[str] = set()
    for item in values:
        result.update(
            token
            for token in re.findall(r"[a-z0-9][a-z0-9+#.-]{1,}", str(item).lower())
            if token not in _STOP_WORDS
        )
    return result


class AgentMatcher:
    """Suggest the best executor agents for a task.

    Score weights are deliberately explicit: capability overlap is the primary
    signal, followed by historical success, availability/load, and cost tier.
    """

    def __init__(self, db: Session):
        self.db = db

    def suggest_agents(self, task: Task, top_n: int = 3) -> list[AgentSuggestion]:
        if top_n <= 0:
            return []

        agents = (
            self.db.query(Agent)
            .filter(Agent.status.notin_(_UNAVAILABLE_STATUSES))
            .all()
        )
        if not agents:
            return []

        task_terms = self._task_terms(task)
        load_by_agent = self._active_loads()
        suggestions: list[tuple[float, AgentSuggestion]] = []

        for agent in agents:
            skill_match, matched_skills = self._skill_match(agent, task_terms)
            performance = self._performance(agent, task, task_terms)
            load = self._load_score(load_by_agent.get(agent.id, 0))
            cost = self._cost_score(agent)
            score = (
                skill_match * 0.45
                + performance * 0.30
                + load * 0.15
                + cost * 0.10
            )
            reason = self._reason(
                agent,
                skill_match,
                performance,
                load_by_agent.get(agent.id, 0),
                cost,
                matched_skills,
            )
            suggestions.append(
                (
                    score,
                    AgentSuggestion(
                        agent_id=agent.id,
                        score=round(max(0.0, min(score, 1.0)), 2),
                        reason=reason,
                    ),
                )
            )

        suggestions.sort(key=lambda item: (-item[0], item[1].agent_id))
        return [suggestion for _, suggestion in suggestions[:top_n]]

    @staticmethod
    def _task_terms(task: Task) -> set[str]:
        fields = (
            task.title,
            task.raw_input,
            task.project,
            task.priority,
            task.risk,
            task.current_gate,
            task.plan,
            getattr(task, "tags", None),
            task.files,
            task.tests,
            task.acceptance_criteria,
            task.flows,
        )
        return set().union(*(_tokens(field) for field in fields))

    @staticmethod
    def _agent_terms(agent: Agent) -> set[str]:
        fields = (
            agent.capabilities,
            agent.role,
            agent.name,
            agent.model,
            agent.effort,
        )
        return set().union(*(_tokens(field) for field in fields))

    def _skill_match(self, agent: Agent, task_terms: set[str]) -> tuple[float, list[str]]:
        agent_terms = self._agent_terms(agent)
        if not task_terms or not agent_terms:
            return 0.5, []
        matched = sorted(task_terms & agent_terms)
        return min(1.0, len(matched) / max(1, min(len(task_terms), 6))), matched[:3]

    def _performance(self, agent: Agent, task: Task, task_terms: set[str]) -> float:
        runs = (
            self.db.query(AgentRun, Task)
            .join(Task, AgentRun.task_id == Task.id)
            .filter(AgentRun.agent_id == agent.id)
            .filter(AgentRun.status.in_(("success", "failed", "timeout")))
            .all()
        )
        similar_results: list[bool] = []
        for run, previous_task in runs:
            previous_terms = self._task_terms(previous_task)
            overlap = len(task_terms & previous_terms) / max(1, len(task_terms))
            if overlap >= 0.2:
                similar_results.append(run.status == "success")

        if similar_results:
            return sum(similar_results) / len(similar_results)

        configured_rate = agent.success_rate
        if configured_rate is None:
            return 0.5
        # Agent rows created before performance data is imported use 0.0 as the
        # column default. Treat that value as unknown unless runs establish it.
        return 0.5 if configured_rate == 0.0 and not runs else max(0.0, min(configured_rate, 1.0))

    def _active_loads(self) -> dict[str, int]:
        rows = (
            self.db.query(AgentRun.agent_id, AgentRun.status)
            .filter(AgentRun.status.in_(_ACTIVE_RUN_STATUSES))
            .all()
        )
        loads = Counter(agent_id for agent_id, _ in rows)
        # Imported/legacy installations may only maintain Agent.status rather
        # than AgentRun rows, so preserve that signal as one active task.
        for (agent_id,) in self.db.query(Agent.id).filter(Agent.status == "busy").all():
            loads[agent_id] = max(loads[agent_id], 1)
        return loads

    @staticmethod
    def _load_score(active_runs: int) -> float:
        return 1.0 / (1.0 + active_runs)

    @staticmethod
    def _cost_score(agent: Agent) -> float:
        effort = (agent.effort or "").lower()
        model = f"{agent.model or ''} {agent.name or ''}".lower()
        if effort == "low" or "flash" in model or "mini" in model:
            return 1.0
        if effort == "medium":
            return 0.7
        if effort in {"high", "extra-high", "max", "ultra"} or any(
            name in model for name in ("opus", "pro")
        ):
            return 0.35
        return 0.6

    @staticmethod
    def _reason(
        agent: Agent,
        skill_match: float,
        performance: float,
        active_runs: int,
        cost: float,
        matched_skills: list[str],
    ) -> str:
        if matched_skills:
            return f"Matches {', '.join(matched_skills)}; {performance:.0%} success on similar tasks"
        if performance >= 0.8:
            return f"High success rate ({performance:.0%}) on similar tasks"
        if cost >= 0.9:
            return "Fast, low-cost agent"
        if active_runs == 0:
            return "Available with balanced performance and cost"
        return f"Available; {active_runs} active task{'s' if active_runs != 1 else ''}"
