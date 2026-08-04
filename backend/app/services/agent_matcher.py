"""Rank available agents for a task.

The matcher intentionally uses explainable, database-backed signals.  This keeps
suggestions useful before an embeddings/indexing service is available and makes
the score stable enough to show in the dispatch UI.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Agent, AgentAccount, AgentRun, Task
from app.schemas.agent import AgentSuggestion

# Bumped whenever the scoring formula (weights, signals) changes, so
# DispatchDecision.policy_version tells you which formula produced a given
# selected_score/final_score (CTV2-202).
POLICY_VERSION = "agent_matcher_v2"

_COMPLETED_RUN_STATUSES = ("success", "failed", "timeout")


@dataclass
class CandidateScore:
    """One agent's eligibility and score breakdown for a task (CTV2-202)."""

    agent_id: str
    eligible: bool
    rejection_reason: str | None = None
    skill_match: float | None = None
    performance: float | None = None
    load: float | None = None
    cost: float | None = None
    work_type_fit: float | None = None
    risk_fit: float | None = None
    final_score: float | None = None
    predicted_pass1: float | None = None
    predicted_runtime: float | None = None
    quota_pressure: float | None = None
    reason: str | None = None
    matched_skills: list[str] = field(default_factory=list)


@dataclass
class ScoringResult:
    """Full output of a scoring pass: every candidate plus the ranked top-N."""

    feature_snapshot: dict[str, Any]
    candidates: list[CandidateScore]
    suggestions: list[AgentSuggestion]


_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "to",
    "with", "task", "this", "new", "add", "update", "implement",
}
_ACTIVE_RUN_STATUSES = ("queued", "running")
_UNAVAILABLE_STATUSES = {"offline", "deprecated", "disabled"}
# Retired API-backed planner profiles. They are kept as historical rows for
# auditability, but must never be selected after the CLI-only planner cutover.
RETIRED_AGENT_IDS = frozenset({"spec-planner-api", "spec-planner-glm"})

# CT v1 lesson: research/review work should route to agents who advertise
# that strength explicitly, not just whoever scores highest on generic
# skill overlap. "execute" is the default and falls back to task domain tags.
_ROUTED_WORK_TYPES = ("research", "review")
_HIGH_EFFORT = {"high", "extra-high", "max", "ultra"}
# CT v1 lesson: large blast radius or explicit high risk should escalate to
# a stronger model, since a cheap agent guessing wrong is expensive to undo.
_RISK_FILE_THRESHOLD = 8


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

    def suggest_agents(
        self,
        task: Task,
        top_n: int = 3,
        *,
        required_capabilities: set[str] | None = None,
        exclude_agent_id: str | None = None,
    ) -> list[AgentSuggestion]:
        if top_n <= 0:
            return []
        return self.score_candidates(
            task,
            top_n,
            required_capabilities=required_capabilities,
            exclude_agent_id=exclude_agent_id,
        ).suggestions

    def score_candidates(
        self,
        task: Task,
        top_n: int = 3,
        *,
        required_capabilities: set[str] | None = None,
        exclude_agent_id: str | None = None,
    ) -> ScoringResult:
        """Score every agent (eligible or not) for a task (CTV2-202).

        Unlike :meth:`suggest_agents`, ineligible agents are kept in the
        result (with a rejection reason) rather than filtered out, so callers
        that persist a DispatchDecision have the full candidate pool to
        explain why an agent was or wasn't picked.
        """
        agents = self.db.query(Agent).all()
        task_terms = self._task_terms(task)
        work_type = self._work_type(task)
        risk_escalated = self._is_risk_escalated(task)
        load_by_agent = self._active_loads()
        account_by_agent = self._accounts_by_agent()
        excluded = exclude_agent_id.strip().casefold() if exclude_agent_id else None
        feature_snapshot = self._feature_snapshot(task, work_type, risk_escalated)

        candidates: list[CandidateScore] = []
        scored: list[tuple[float, AgentSuggestion]] = []

        for agent in agents:
            ineligible_reason = self._ineligibility_reason(
                agent, excluded, required_capabilities
            )
            if ineligible_reason is not None:
                candidates.append(
                    CandidateScore(
                        agent_id=agent.id,
                        eligible=False,
                        rejection_reason=ineligible_reason,
                    )
                )
                continue

            skill_match, matched_skills = self._skill_match(agent, task_terms)
            performance = self._performance(agent, task, task_terms)
            active_runs = load_by_agent.get(agent.id, 0)
            load = self._load_score(active_runs)
            quota_pressure = self._quota_pressure(account_by_agent.get(agent.id))
            cost = self._cost_score(agent)
            work_type_fit = self._work_type_boost(agent, work_type, task_terms)
            risk_fit = self._risk_escalation(agent, risk_escalated)
            score = (
                skill_match * 0.30
                + performance * 0.25
                + load * 0.10
                + cost * 0.10
                + work_type_fit * 0.15
                + risk_fit * 0.05
                + (1.0 - quota_pressure) * 0.05
            )
            final_score = round(max(0.0, min(score, 1.0)), 2)
            reason = self._reason(
                agent, skill_match, performance, active_runs, cost, matched_skills
            )
            candidates.append(
                CandidateScore(
                    agent_id=agent.id,
                    eligible=True,
                    skill_match=skill_match,
                    performance=performance,
                    load=load,
                    cost=cost,
                    work_type_fit=work_type_fit,
                    risk_fit=risk_fit,
                    final_score=final_score,
                    predicted_pass1=performance,
                    predicted_runtime=self._predicted_runtime(agent.id),
                    quota_pressure=round(quota_pressure, 4),
                    reason=reason,
                    matched_skills=matched_skills,
                )
            )
            scored.append((final_score, AgentSuggestion(agent_id=agent.id, score=final_score, reason=reason)))

        scored.sort(key=lambda item: (-item[0], item[1].agent_id))
        suggestions = [suggestion for _, suggestion in scored[: max(0, top_n)]]
        return ScoringResult(
            feature_snapshot=feature_snapshot, candidates=candidates, suggestions=suggestions
        )

    @staticmethod
    def _ineligibility_reason(
        agent: Agent,
        excluded: str | None,
        required_capabilities: set[str] | None,
    ) -> str | None:
        if agent.id.strip().casefold() in RETIRED_AGENT_IDS:
            return "agent profile is retired (CLI-only planner cutover)"
        if agent.status in _UNAVAILABLE_STATUSES:
            return f"agent status is {agent.status}"
        if excluded and agent.id.strip().casefold() == excluded:
            return "excluded from candidate pool (e.g. four-eyes)"
        if required_capabilities and not (
            set(agent.normalized_capabilities) & required_capabilities
        ):
            return (
                "missing required capability: "
                f"{', '.join(sorted(required_capabilities))}"
            )
        return None

    @staticmethod
    def _feature_snapshot(task: Task, work_type: str, risk_escalated: bool) -> dict[str, Any]:
        return {
            "task_id": task.id,
            "priority": task.priority,
            "risk": task.risk,
            "tags": list(getattr(task, "tags", None) or []),
            "files_count": len(task.files or []),
            "tests_count": len(task.tests or []),
            "work_type": work_type,
            "risk_escalated": risk_escalated,
        }

    def _predicted_runtime(self, agent_id: str) -> float | None:
        runs = (
            self.db.query(AgentRun.started_at, AgentRun.completed_at)
            .filter(
                AgentRun.agent_id == agent_id,
                AgentRun.status.in_(_COMPLETED_RUN_STATUSES),
                AgentRun.started_at.isnot(None),
                AgentRun.completed_at.isnot(None),
            )
            .all()
        )
        durations = [
            (completed - started).total_seconds() for started, completed in runs
        ]
        if not durations:
            return None
        return round(sum(durations) / len(durations), 2)

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
            agent.normalized_capabilities,
            agent.normalized_roles,
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

    @staticmethod
    def _work_type(task: Task) -> str:
        """Infer research/review/execute routing from the task's tags.

        Tasks have no dedicated ``type`` column, so the work type is read
        from ``tags`` (falling back to an explicit ``type`` attribute if one
        is ever added) and defaults to "execute".
        """
        explicit = getattr(task, "type", None)
        if explicit in _ROUTED_WORK_TYPES:
            return explicit
        tags = {str(tag).lower() for tag in (getattr(task, "tags", None) or [])}
        for work_type in _ROUTED_WORK_TYPES:
            if work_type in tags:
                return work_type
        return "execute"

    @staticmethod
    def _work_type_boost(agent: Agent, work_type: str, task_terms: set[str]) -> float:
        capabilities = {str(c).lower() for c in agent.normalized_capabilities}
        if work_type in _ROUTED_WORK_TYPES:
            return 1.0 if work_type in capabilities else 0.3
        domain_terms = task_terms - set(_ROUTED_WORK_TYPES)
        if not domain_terms or not capabilities:
            return 0.5
        return 1.0 if capabilities & domain_terms else 0.3

    @staticmethod
    def _is_risk_escalated(task: Task) -> bool:
        risk = (getattr(task, "risk", None) or "").lower()
        files = getattr(task, "files", None) or []
        return risk == "high" or len(files) > _RISK_FILE_THRESHOLD

    @staticmethod
    def _risk_escalation(agent: Agent, risk_escalated: bool) -> float:
        if not risk_escalated:
            # Neutral for every agent so low-risk tasks aren't skewed by effort.
            return 0.5
        effort = (agent.effort or "").lower()
        if effort in _HIGH_EFFORT:
            return 1.0
        if effort == "medium":
            return 0.5
        return 0.1

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

    def _accounts_by_agent(self) -> dict[str, AgentAccount]:
        accounts = self.db.query(AgentAccount).all()
        result: dict[str, AgentAccount] = {}
        for account in accounts:
            current = result.get(account.agent_id)
            if current is None or account.quota_pressure > current.quota_pressure:
                result[account.agent_id] = account
        return result

    @staticmethod
    def _quota_pressure(account: AgentAccount | None) -> float:
        return max(0.0, min(1.0, float(account.quota_pressure or 0.0))) if account else 0.0

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
