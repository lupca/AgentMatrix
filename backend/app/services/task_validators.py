"""Validators, brake policies, autonomy resolution, and prerequisite logic for task orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Agent,
    AgentAccount,
    AgentRun,
    AuditLog,
    GateRecord,
    LLMUsage,
    Project,
    RunResourceUsage,
    Setting,
    Task,
    TaskDependency,
)

logger = logging.getLogger(__name__)


class OrchestrationError(RuntimeError):
    """Base error for a rejected orchestration intent."""


class TaskNotFoundError(OrchestrationError):
    pass


class TransitionConflictError(OrchestrationError):
    pass


class ModeViolationError(OrchestrationError):
    pass


class PrerequisiteError(OrchestrationError):
    pass


class IdempotencyConflictError(OrchestrationError):
    pass


class StaleIdempotencyRecordError(OrchestrationError):
    """A cached gate record refers to a run that is no longer active."""


class BrakeViolationError(OrchestrationError):
    """An autonomy or budget brake stopped forward progress."""


class DependencyCycleError(OrchestrationError):
    """A requested task_dependencies edge would close a cycle (or self-loop)."""


@dataclass(frozen=True)
class BrakeDecision:
    allowed: bool
    reason: str | None = None
    code: str | None = None
    queue: bool = False
    cost_usd: Decimal = Decimal("0")
    retry_after_seconds: int | None = None
    observations: dict[str, Any] | None = None


@dataclass(frozen=True)
class AutonomyPolicy:
    autonomy: str = "supervised"
    auto_max_risk: str = "normal"
    auto_max_rounds: int = 3


class TaskValidator:
    """Validation rules, autonomy policy resolution, brake checks, and dependency graph inspection."""

    MODES = {"supervised", "plan-only", "bypass"}
    _RISK_LEVELS = {"low": 0, "normal": 1, "high": 2}
    _AUTONOMY_VALUES = {"plan-only", "supervised", "auto"}
    _DEAD_RUN_STATUSES = {"success", "timeout", "cancelled"}
    _UNAVAILABLE_REVIEWER_STATUSES = {"disabled", "offline", "deprecated"}

    def __init__(self, db: Session):
        self.db = db

    def resolve_autonomy(self, project: Project | str | None) -> AutonomyPolicy:
        """Resolve project policy over global settings, failing safe per key."""
        project_row = (
            project
            if isinstance(project, Project)
            else (self.db.get(Project, project) if project is not None else None)
        )
        override = project_row.autonomy_policy if project_row is not None else None
        override = override if isinstance(override, dict) else {}

        def value(key: str, default: Any) -> Any:
            if key in override:
                return override[key]
            row = self.db.get(Setting, key)
            return default if row is None else row.value

        autonomy = value("autonomy", "supervised")
        if not isinstance(autonomy, str) or autonomy.strip().lower() not in self._AUTONOMY_VALUES:
            autonomy = "supervised"
        else:
            autonomy = autonomy.strip().lower()

        max_risk = value("auto_max_risk", "normal")
        if not isinstance(max_risk, str) or max_risk.strip().lower() not in {"low", "normal"}:
            max_risk = "normal"
        else:
            max_risk = max_risk.strip().lower()

        try:
            max_rounds = int(value("auto_max_rounds", 3))
        except (TypeError, ValueError):
            max_rounds = 3
        max_rounds = max(1, max_rounds)
        return AutonomyPolicy(autonomy, max_risk, max_rounds)

    def mode_for_task(self, task: Task, *, risk: str | None = None) -> str:
        if task.mode in {"plan-only", "bypass"}:
            return task.mode
        policy = self.resolve_autonomy(task.project)
        if policy.autonomy == "plan-only":
            return "plan-only"
        if policy.autonomy != "auto":
            return "supervised"
        normalized_risk = (risk if risk is not None else task.risk or "").strip().lower()
        if normalized_risk not in self._RISK_LEVELS:
            return "supervised"
        if self._RISK_LEVELS[normalized_risk] > self._RISK_LEVELS[policy.auto_max_risk]:
            return "supervised"
        return "bypass"

    @property
    def autonomy_enabled(self) -> bool:
        return self._setting("autonomy_enabled", settings.AUTONOMY_ENABLED, bool)

    @property
    def max_cost_usd_per_task(self) -> Decimal:
        val = self._setting("max_cost_usd_per_task", settings.MAX_COST_USD_PER_TASK, Decimal)
        return max(Decimal("0"), val)

    @property
    def max_concurrent_runs(self) -> int:
        return max(1, self._setting("max_concurrent_runs", settings.MAX_CONCURRENT_RUNS, int))

    @property
    def run_timeout_seconds(self) -> int:
        return max(1, self._setting("agent_run_timeout_seconds", settings.RUN_TIMEOUT_SECONDS, int))

    @property
    def max_active_seconds_per_run(self) -> int:
        return max(1, self._setting("max_active_seconds_per_run", settings.MAX_ACTIVE_SECONDS_PER_RUN, int))

    @property
    def max_tool_calls_per_run(self) -> int:
        return max(1, self._setting("max_tool_calls_per_run", settings.MAX_TOOL_CALLS_PER_RUN, int))

    @property
    def max_no_progress_seconds(self) -> int:
        return max(1, self._setting("max_no_progress_seconds", settings.MAX_NO_PROGRESS_SECONDS, int))

    def check_brakes(
        self,
        task: Task,
        *,
        for_spawn: bool = False,
        audit: bool = False,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> BrakeDecision:
        """Evaluate brakes in a stable order and return debugging context."""
        cost = self._task_cost(task)
        active_query = self.db.query(AgentRun.id).filter(AgentRun.status.in_(["queued", "running"]))
        if run_id:
            active_query = active_query.filter(AgentRun.id != run_id)
        active = len(active_query.order_by(AgentRun.id).with_for_update().all()) if for_spawn else int(
            self.db.query(AgentRun.id).filter(AgentRun.status.in_(["queued", "running"])).count()
        )
        observations: dict[str, Any] = {
            "active_runs": active,
            "max_concurrent": self.max_concurrent_runs,
            "task_cost": str(cost),
            "cost_limit": str(self.max_cost_usd_per_task),
            "agent_id": agent_id,
            "max_active_seconds_per_run": self.max_active_seconds_per_run,
            "max_tool_calls_per_run": self.max_tool_calls_per_run,
            "max_no_progress_seconds": self.max_no_progress_seconds,
        }
        dep_ids = self.dependency_ids(task.id)
        deps = list(self.db.query(Task).filter(Task.id.in_(dep_ids)).all()) if dep_ids else []
        pending_deps = [d for d in deps if d.status not in {"done", "failed"}]
        if task.status in {"done", "cancelled"}:
            decision = BrakeDecision(False, f"Task is terminal: {task.status}", "terminal", observations=observations)
        elif task.awaiting_approval:
            decision = BrakeDecision(False, "Task has a pending gate", "pending_gate", observations=observations)
        elif pending_deps:
            dep_ids_str = ", ".join(str(d.id) for d in pending_deps[:3])
            decision = BrakeDecision(False, f"Waiting for dependencies: {dep_ids_str}", "dependency_pending", queue=True, observations=observations)
        elif not self.autonomy_enabled:
            decision = BrakeDecision(False, "Autonomy is disabled", "autonomy_disabled", observations=observations)
        elif cost >= self.max_cost_usd_per_task:
            reason = f"Task cost limit reached: ${cost:.8f} >= ${self.max_cost_usd_per_task:.8f}"
            decision = BrakeDecision(False, reason, "cost_limit", cost_usd=cost, observations=observations)
        else:
            agent = self.db.get(Agent, agent_id) if agent_id else None
            if agent_id and agent is None:
                decision = BrakeDecision(False, f"Agent {agent_id} not found", "agent_capability", observations=observations)
            elif agent is not None and agent.status not in {"idle", "active"}:
                decision = BrakeDecision(False, f"Agent {agent.id} is unavailable: {agent.status}", "agent_capability", retry_after_seconds=60, observations=observations)
            else:
                account = self.db.query(AgentAccount).filter(AgentAccount.agent_id == agent_id).first() if agent_id else None
                if account is not None and (account.status not in {"healthy", "active"} or account.health_score <= 0):
                    decision = BrakeDecision(False, f"Agent account is unhealthy: {account.status}", "account_health", queue=True, retry_after_seconds=60, observations=observations)
                elif run_id and (run := self.db.get(AgentRun, run_id)) is not None:
                    usage = self.db.get(RunResourceUsage, run_id)
                    active_seconds = float(usage.active_seconds if usage else 0)
                    tool_calls = int(usage.tool_calls if usage else 0)
                    last_activity = run.updated_at or run.started_at
                    if last_activity is None:
                        no_progress_seconds = 0
                    else:
                        if last_activity.tzinfo is None:
                            last_activity = last_activity.replace(tzinfo=timezone.utc)
                        no_progress_seconds = max(0, int((datetime.now(timezone.utc) - last_activity).total_seconds()))
                    observations.update({"active_seconds": active_seconds, "tool_calls": tool_calls, "no_progress_seconds": no_progress_seconds})
                    if active_seconds >= self.max_active_seconds_per_run:
                        decision = BrakeDecision(False, "Run active-time limit reached", "active_time_limit", observations=observations)
                    elif tool_calls >= self.max_tool_calls_per_run:
                        decision = BrakeDecision(False, "Run tool-call limit reached", "tool_calls_limit", observations=observations)
                    elif no_progress_seconds >= self.max_no_progress_seconds:
                        decision = BrakeDecision(False, "Run made no progress within the allowed interval", "no_progress_limit", retry_after_seconds=60, observations=observations)
                    elif for_spawn and active >= self.max_concurrent_runs:
                        decision = BrakeDecision(False, f"Concurrent run limit reached: {active} >= {self.max_concurrent_runs}", "concurrency_limit", queue=True, retry_after_seconds=30, cost_usd=cost, observations=observations)
                    else:
                        decision = BrakeDecision(True, cost_usd=cost, observations=observations)
                elif for_spawn and active >= self.max_concurrent_runs:
                    decision = BrakeDecision(False, f"Concurrent run limit reached: {active} >= {self.max_concurrent_runs}", "concurrency_limit", queue=True, retry_after_seconds=30, cost_usd=cost, observations=observations)
                else:
                    decision = BrakeDecision(True, cost_usd=cost, observations=observations)

        if audit:
            self._record_brake(task, decision)
        return decision

    def _record_brake(self, task: Task, decision: BrakeDecision) -> None:
        if decision.code in {"autonomy_disabled", "cost_limit"}:
            from app.services.task_state_machine import TaskStateMachine
            TaskStateMachine(self.db).cas_status(task, "failed")
            task.error = decision.reason
            task.awaiting_approval = True
            task.approval_prompt = f"Escalated by safety brake: {decision.reason}"
            payload = {"code": decision.code, "reason": decision.reason}
            inp_hash = self.input_hash(payload)
            record = GateRecord(
                task_id=task.id,
                gate_type="safety_brake",
                status="rejected",
                actor="system:safety-brake",
                mode=task.mode,
                idempotency_key=str(uuid.uuid4()),
                input_hash=inp_hash,
                executor=task.executor,
                reviewer=task.reviewer,
                input_payload=payload,
                error_message=decision.reason,
            )
            self.db.add(record)
            self.db.flush()
            self.db.add(
                AuditLog(
                    task_id=task.id,
                    action=f"transition:{record.gate_type}:{record.status}",
                    actor=record.actor,
                    details={
                        "gate_record_id": record.id,
                        "idempotency_key": record.idempotency_key,
                        "input_hash": record.input_hash,
                        "mode": record.mode,
                        "status": task.status,
                        "reason": decision.reason,
                    },
                )
            )
        if not decision.allowed:
            self.db.add(
                AuditLog(
                    task_id=task.id,
                    action=f"brake:{decision.code}",
                    actor="system:safety-brake",
                    details={
                        "code": decision.code,
                        "reason": decision.reason,
                        "cost_usd": str(decision.cost_usd),
                        "max_cost_usd_per_task": str(self.max_cost_usd_per_task),
                        "max_concurrent_runs": self.max_concurrent_runs,
                        "decision": self._json_safe(asdict(decision)),
                    },
                )
            )
        self.db.commit()
        if decision.code in {"autonomy_disabled", "cost_limit"}:
            from app.services.task_state_machine import TaskStateMachine
            TaskStateMachine(self.db).wake_dependents(task.id)

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, dict):
            return {key: TaskValidator._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [TaskValidator._json_safe(item) for item in value]
        return value

    def _task_cost(self, task: Task) -> Decimal:
        value = (
            self.db.query(func.coalesce(func.sum(LLMUsage.cost_usd), 0))
            .outerjoin(AgentRun, LLMUsage.agent_run_id == AgentRun.id)
            .filter(or_(LLMUsage.task_id == task.id, AgentRun.task_id == task.id))
            .scalar()
        )
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def _setting(self, key: str, default: Any, converter: Any) -> Any:
        row = self.db.get(Setting, key)
        value = default if row is None else row.value
        if converter is bool:
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        try:
            return converter(value)
        except (TypeError, ValueError, InvalidOperation):
            return default

    def validate_verdict_prerequisites(
        self,
        task: Task,
        *,
        actor: str,
        verdict: str,
        ac_results: Any,
    ) -> None:
        self.assert_status(task, "in-review")
        if not task.executor or not task.executor.strip():
            raise PrerequisiteError("executor is required for verdict")
        if not task.reviewer or not task.reviewer.strip():
            raise PrerequisiteError("reviewer is required for verdict")
        self.require_independent(task.executor, task.reviewer)
        review_run = (
            self.db.query(AgentRun)
            .filter(
                AgentRun.task_id == task.id,
                AgentRun.kind == "review",
                AgentRun.status == "success",
            )
            .order_by(AgentRun.queued_at.desc())
            .first()
        )
        if review_run is None:
            raise PrerequisiteError(
                "verdict requires a completed review run for this task"
            )
        if self.principal(review_run.agent_id) != self.principal(task.reviewer):
            raise PrerequisiteError(
                "The completed review run's agent does not match the task's "
                "assigned reviewer"
            )
        if not task.result_ref or not task.result_ref.strip():
            raise PrerequisiteError("result_ref is required for verdict")
        evaluations = self.evaluation_results(ac_results)
        required_count = len(task.acceptance_criteria or [])
        if len(evaluations) < required_count:
            raise PrerequisiteError(
                "Acceptance-criteria evaluation results are incomplete"
            )
        if verdict == "pass" and not all(evaluations):
            raise PrerequisiteError(
                "A passing verdict requires every acceptance criterion to pass"
            )

    @staticmethod
    def evaluation_results(ac_results: Any) -> list[bool]:
        if isinstance(ac_results, dict):
            values = list(ac_results.values())
        elif isinstance(ac_results, list):
            values = ac_results
        else:
            raise PrerequisiteError(
                "Acceptance-criteria evaluation results are required"
            )
        if not values:
            raise PrerequisiteError(
                "Acceptance-criteria evaluation results are required"
            )
        results: list[bool] = []
        for value in values:
            if isinstance(value, bool):
                results.append(value)
                continue
            if isinstance(value, dict):
                if isinstance(value.get("passed"), bool):
                    results.append(value["passed"])
                    continue
                status = str(value.get("status", "")).strip().lower()
                if status in {"pass", "passed", "met", "fail", "failed", "unmet"}:
                    results.append(status in {"pass", "passed", "met"})
                    continue
            raise PrerequisiteError(
                "Each acceptance-criteria result needs a boolean passed value"
            )
        return results

    def task(self, task_id: str) -> Task:
        task = (
            self.db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        return task

    def idempotent_record(
        self,
        task_id: str,
        idempotency_key: str,
        input_hash: str,
    ) -> GateRecord | None:
        record = (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.idempotency_key == idempotency_key,
            )
            .first()
        )
        if record is not None and record.input_hash != input_hash:
            raise IdempotencyConflictError(
                f"Idempotency key {idempotency_key!r} was reused with different input"
            )
        return record

    def reject_if_stale_dispatch_record(self, record: GateRecord) -> None:
        effective = record
        if effective.status == "pending":
            decision = (
                self.db.query(GateRecord)
                .filter(GateRecord.parent_id == effective.id)
                .order_by(GateRecord.id.desc())
                .first()
            )
            if decision is not None:
                effective = decision
        if effective.gate_type != "dispatch" or effective.status != "approved":
            return
        if not effective.output_ref:
            return
        run = self.db.get(AgentRun, effective.output_ref)
        if run is None:
            return
        is_terminal = run.status in self._DEAD_RUN_STATUSES or (
            run.status == "failed" and run.attempt >= run.max_attempts
        )
        if is_terminal:
            raise StaleIdempotencyRecordError(
                f"Idempotency key {effective.idempotency_key!r} refers to run "
                f"{run.id!r} which is already terminal (status={run.status!r}, "
                f"attempt={run.attempt}/{run.max_attempts}); retry dispatch "
                "with a new idempotency key"
            )

    @classmethod
    def validate_common(
        cls,
        task: Task,
        actor: str,
        idempotency_key: str,
    ) -> None:
        if task.mode not in cls.MODES:
            raise ModeViolationError(f"Unsupported task mode: {task.mode}")
        if not actor or not actor.strip():
            raise PrerequisiteError("actor is required")
        if not idempotency_key or not idempotency_key.strip():
            raise PrerequisiteError("idempotency_key is required")
        if len(idempotency_key) > 100:
            raise PrerequisiteError("idempotency_key must be at most 100 characters")

    @staticmethod
    def assert_status(task: Task, expected_status: str) -> None:
        if task.status != expected_status:
            raise TransitionConflictError(
                f"Task {task.id} expected status {expected_status!r}, "
                f"found {task.status!r}"
            )

    @classmethod
    def require_independent(
        cls,
        executor: str | None,
        reviewer: str | None,
    ) -> None:
        if not executor or not executor.strip():
            raise PrerequisiteError("executor is required")
        if not reviewer or not reviewer.strip():
            raise PrerequisiteError("reviewer is required")
        if cls.principal(executor) == cls.principal(reviewer):
            raise PrerequisiteError("Reviewer must differ from executor")

    @staticmethod
    def principal(value: str) -> str:
        return value.strip().casefold()

    @staticmethod
    def input_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def creates_cycle(self, task_id: str, depends_on_task_id: str) -> bool:
        adjacency: dict[str, list[str]] = {}
        for edge in self.db.query(TaskDependency).all():
            adjacency.setdefault(edge.task_id, []).append(edge.depends_on_task_id)

        stack = [depends_on_task_id]
        visited: set[str] = set()
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            stack.extend(adjacency.get(current, []))
        return False

    def unmet_dependencies(self, task_id: str) -> list[Task]:
        dep_ids = self.dependency_ids(task_id)
        if not dep_ids:
            return []
        return (
            self.db.query(Task)
            .filter(Task.id.in_(dep_ids), Task.status != "done")
            .all()
        )

    def failed_dependencies(self, task_id: str) -> list[str]:
        dep_ids = self.dependency_ids(task_id)
        if not dep_ids:
            return []
        found = {
            row.id: row
            for row in self.db.query(Task).filter(Task.id.in_(dep_ids)).all()
        }
        return [
            dep_id
            for dep_id in dep_ids
            if dep_id not in found or found[dep_id].status == "failed"
        ]

    def dependency_ids(self, task_id: str) -> list[str]:
        return [
            row.depends_on_task_id
            for row in self.db.query(TaskDependency.depends_on_task_id)
            .filter(TaskDependency.task_id == task_id)
            .all()
        ]

    def dependent_task_ids(self, task_id: str) -> list[str]:
        return [
            row.task_id
            for row in self.db.query(TaskDependency.task_id)
            .filter(TaskDependency.depends_on_task_id == task_id)
            .all()
        ]
