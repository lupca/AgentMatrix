"""Validators, brake policies, autonomy resolution, and prerequisite logic for task orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import psutil
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
    ReviewCycle,
    RunResourceUsage,
    Setting,
    Task,
    TaskDependency,
)
from app.services.approval_hold import derive_approval_hold
from app.services.review_criteria import merged_review_criteria

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


def _is_pid_alive_and_active(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    try:
        proc = psutil.Process(pid)
        if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            return False
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True


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
    def max_tokens_per_task(self) -> int:
        val = self._setting("max_tokens_per_task", settings.MAX_TOKENS_PER_TASK, int)
        return max(0, val)

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
        tokens = self._task_tokens(task)
        # `task.limits` is the PLANNER's own estimate of what the task should
        # cost.  It is advisory, never a hard stop.
        #
        # It used to be min()'d into the enforced limit, which let one number
        # invented during a single plan generation permanently brick the task.
        # CTV2-1388, 2026-08-05: the planner wrote `max_tokens: 12000`; the task
        # had already spent 282k on the planner rounds themselves, so every
        # subsequent run died with "Task token limit reached: 282,028 >= 12,000".
        # And there was no way out -- `limits` is only writable by
        # generate_spec_plan, which was the very thing being blocked, and
        # update_task refuses the field.  A task could sentence itself to death
        # and then be unable to appeal.
        #
        # The operator-configured limits below are the real brakes.  The plan's
        # numbers are reported in `observations` so overruns stay visible.
        plan_limits = task.limits if isinstance(task.limits, dict) else {}
        token_limit = self.max_tokens_per_task
        cost_limit = self.max_cost_usd_per_task
        plan_token_limit = plan_limits.get("max_tokens")
        plan_cost_limit = plan_limits.get("max_cost_usd")
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
            "cost_limit": str(cost_limit),
            "task_tokens": tokens,
            "token_limit": token_limit,
            # Advisory only -- surfaced so an overrun against the plan's own
            # estimate is visible without being able to stop the task.
            "plan_token_estimate": plan_token_limit,
            "plan_cost_estimate": str(plan_cost_limit) if plan_cost_limit is not None else None,
            "over_plan_token_estimate": bool(
                isinstance(plan_token_limit, int)
                and plan_token_limit > 0
                and tokens >= plan_token_limit
            ),
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
        elif (hold := derive_approval_hold(self.db, task)) is not None:
            # Derived, not read off `task.awaiting_approval` (CTV2-1401): a
            # stored flag that had drifted off the ledger used to stop every
            # run forever, with no tool able to clear it.  The reason names
            # which hold it is, so the caller knows which tool resolves it.
            reason = (
                "Task has a pending gate"
                if hold.source == "gate"
                else f"Task is waiting on a human ({hold.source})"
            )
            decision = BrakeDecision(
                False,
                f"{reason}: {hold.prompt}",
                "pending_gate",
                observations=observations,
            )
        elif pending_deps:
            dep_ids_str = ", ".join(str(d.id) for d in pending_deps[:3])
            decision = BrakeDecision(False, f"Waiting for dependencies: {dep_ids_str}", "dependency_pending", queue=True, observations=observations)
        elif not self.autonomy_enabled:
            decision = BrakeDecision(False, "Autonomy is disabled", "autonomy_disabled", observations=observations)
        elif cost >= cost_limit:
            reason = f"Task cost limit reached: ${cost:.8f} >= ${cost_limit:.8f}"
            decision = BrakeDecision(False, reason, "cost_limit", cost_usd=cost, observations=observations)
        elif tokens >= token_limit:
            reason = f"Task token limit reached: {tokens:,} >= {token_limit:,} tokens"
            decision = BrakeDecision(False, reason, "token_limit", cost_usd=cost, observations=observations)
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
                    if run.status == "running" and run.pid and _is_pid_alive_and_active(run.pid):
                        now_utc = datetime.now(timezone.utc)
                        run.updated_at = now_utc
                        self.db.flush()
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
        # A budget brake exists to stop the task SPENDING MORE.  It must not
        # destroy work that has already been paid for and delivered.
        #
        # 2026-08-05, CTV2-1382 -- one second after the executor's `execution`
        # gate was approved and `result_ref` was written, the token brake fired
        # and flipped the task to `failed`:
        #
        #   05:22:52  gate 1620  execution     approved   agent:@claude-sonnet-high
        #   05:22:53  gate 1621  safety_brake  rejected   system:safety-brake
        #
        # `failed` is terminal, so `_sync_after_transition` cancelled every run
        # and rejected every pending gate.  A commit with 830 passing tests was
        # stranded on its branch with no way back: request_review, land_task,
        # attach_result and critique_spec_plan all refuse to touch a failed task.
        # 56M tokens of good work were discarded for the sole reason that it had
        # cost 56M tokens.
        #
        # When a result already exists, keep the task where it is.  Refusing to
        # spawn the next run (which every caller of check_brakes already does on
        # `allowed=False`) is the whole point of the brake; the status flip was
        # never part of it.
        delivered = bool((task.result_ref or "").strip())
        budget_brake = decision.code in {"autonomy_disabled", "cost_limit", "token_limit"}
        if budget_brake:
            from app.services.task_state_machine import TaskStateMachine
            if not delivered:
                TaskStateMachine(self.db).cas_status(task, "failed")
            task.error = decision.reason
            payload = {
                "code": decision.code,
                "reason": decision.reason,
                # Record which branch was taken so the ledger explains why an
                # identical brake left one task failed and another untouched.
                "result_delivered": delivered,
                "task_status": task.status,
            }
            inp_hash = self.input_hash(payload)
            # `pending` when a result survives, `rejected` when the task is on
            # its way to `failed` (CTV2-1401).
            #
            # The brake used to assert `awaiting_approval` on the side and
            # write a `rejected` record.  That flag was the only thing holding
            # a delivered-but-braked task, and nothing could lower it:
            # `approve_gate` answers "No pending gate found" for a rejected
            # root.  Written as `pending`, the same hold is derived from the
            # ledger, shows up in `pending_approvals`, and a human can
            # actually decide it -- the shape escalations already moved to in
            # CTV2-1389.
            #
            # The undelivered branch keeps `rejected`: that task becomes
            # terminal three lines below, where every pending gate is rejected
            # anyway, and `reopen_task` looks for a rejected root to hang the
            # reopen record off.
            record = GateRecord(
                task_id=task.id,
                gate_type="safety_brake",
                status="pending" if delivered else "rejected",
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
            if not delivered:
                state_machine = TaskStateMachine(self.db)
                state_machine._cancel_active_runs(task)
                state_machine._reject_all_pending_gates(
                    task, f"Task reached terminal state: {task.status}"
                )
            else:
                TaskStateMachine(self.db).sync_awaiting_approval(task)
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
                        "max_tokens_per_task": self.max_tokens_per_task,
                        "max_concurrent_runs": self.max_concurrent_runs,
                        "decision": self._json_safe(asdict(decision)),
                    },
                )
            )
        if decision.code == "cost_limit":
            # CTV2-1400: tiền là của human, máy không quyết thay được -- one
            # of the four Telegram-whitelisted event types.
            from app.services.task_event_service import emit_task_event

            emit_task_event(
                task_id=task.id,
                event_type="cost_brake",
                payload={
                    "task_id": task.id,
                    "cost_usd": str(decision.cost_usd),
                    "max_cost_usd_per_task": str(self.max_cost_usd_per_task),
                    "reason": decision.reason,
                },
                db=self.db,
                kind="decision",
            )
        self.db.commit()
        # Dependents only need waking when this task actually reached a terminal
        # state; a delivered task the brake left alone is still in flight.
        if budget_brake and not delivered:
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
            .filter(
                or_(LLMUsage.task_id == task.id, AgentRun.task_id == task.id),
                LLMUsage.operation != "cli",
            )
            .scalar()
        )
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def _task_tokens(self, task: Task) -> int:
        rows = (
            self.db.query(LLMUsage.input_tokens, LLMUsage.output_tokens)
            .outerjoin(AgentRun, LLMUsage.agent_run_id == AgentRun.id)
            .filter(or_(LLMUsage.task_id == task.id, AgentRun.task_id == task.id))
            .all()
        )
        return sum(
            max(0, int(input_tokens or 0))
            + max(0, int(output_tokens or 0))
            for input_tokens, output_tokens in rows
        )

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
        review_cycle_id: str,
    ) -> ReviewCycle:
        """Validate that a verdict is legal for `review_cycle_id`, and only
        that cycle (CTV2-1379).

        Four-eyes used to be checked on PERSON only: the most recent
        successful review run for the task_id was compared against
        task.reviewer, so a run from an EARLIER round still satisfied it --
        right person, wrong time. This binds the verdict to one specific
        review_cycle and requires, all at once:
          1. the cycle exists, belongs to this task
          2. the cycle's task_round is the task's CURRENT round
          3. its reviewer_agent_run_id points at a successful review AgentRun
          4. that run's agent matches review_cycles.reviewer_id and
             task.reviewer
          5. the cycle is 'submitted'
          6. task.reviewer != task.executor (the original four-eyes check)
        No fallback to "most recent run" -- that fallback was the hole.
        """
        self.assert_status(task, "in-review")
        if not task.executor or not task.executor.strip():
            raise PrerequisiteError("executor is required for verdict")
        if not task.reviewer or not task.reviewer.strip():
            raise PrerequisiteError("reviewer is required for verdict")
        self.require_independent(task.executor, task.reviewer)

        if not review_cycle_id:
            raise PrerequisiteError("review_cycle_id is required for verdict")
        review_cycle = (
            self.db.query(ReviewCycle)
            .filter(ReviewCycle.id == review_cycle_id, ReviewCycle.task_id == task.id)
            .first()
        )
        if review_cycle is None:
            raise PrerequisiteError(
                f"review_cycle {review_cycle_id} does not exist for task {task.id}"
            )
        if review_cycle.task_round_id != task.current_round_id:
            raise PrerequisiteError(
                "review_cycle does not belong to the task's current round -- "
                "verdict must be recorded against the review that ran for "
                "this round, not an earlier one"
            )
        review_run = (
            self.db.query(AgentRun)
            .filter(
                AgentRun.id == review_cycle.reviewer_agent_run_id,
                AgentRun.task_id == task.id,
                AgentRun.kind == "review",
                AgentRun.status == "success",
            )
            .first()
        )
        if review_run is None:
            raise PrerequisiteError(
                "review_cycle has no completed (status=success) review run"
            )
        if self.principal(review_run.agent_id) != self.principal(
            review_cycle.reviewer_id or ""
        ) or self.principal(review_run.agent_id) != self.principal(task.reviewer):
            raise PrerequisiteError(
                "The completed review run's agent does not match the review "
                "cycle's reviewer and/or the task's assigned reviewer"
            )
        if review_cycle.status != "submitted":
            raise PrerequisiteError(
                f"review_cycle status must be 'submitted', found "
                f"'{review_cycle.status}'"
            )
        if not task.result_ref or not task.result_ref.strip():
            raise PrerequisiteError("result_ref is required for verdict")
        evaluations = self.evaluation_results(ac_results)
        required_count = len(
            merged_review_criteria(task.acceptance_criteria, task.constraints)
        )
        if len(evaluations) != required_count:
            raise PrerequisiteError(
                "Review-criteria evaluations are incomplete or extra: count must match "
                "acceptance_criteria + constraints"
            )
        if verdict == "pass" and not all(evaluations):
            raise PrerequisiteError(
                "A passing verdict requires every acceptance criterion to pass"
            )
        return review_cycle

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

    def describe_next_step(self, task: Task) -> dict[str, Any]:
        """Explain, in {state, why, next} shape, what to do from this task's
        current status.

        This is what a stuck orchestrator should get back the moment it hits
        a state-conflict, instead of a bare "expected X found Y" with no way
        out. CTV2-1394, 2026-08-05: an orchestrator sat on a `failed` task for
        40 minutes because the error that stopped it named no tool to try
        next -- the fix was only found by chance, remembering a code comment.
        Modeled on ``tool_argument_validator.describe_problems``: name the
        evidence, then name the tool.
        """
        status = task.status
        result_ref = (task.result_ref or "").strip()
        if status == "failed":
            if result_ref:
                return {
                    "state": status,
                    "why": (
                        f"task is 'failed' but already has result_ref={result_ref!r} "
                        "-- the work was delivered before the failure"
                    ),
                    "next": (
                        "call reopen_task -- it routes a delivered result to "
                        "'awaiting-review' so an independent reviewer still has "
                        "to pass it"
                    ),
                }
            return {
                "state": status,
                "why": "task is 'failed' with no result_ref delivered yet",
                "next": (
                    "call reopen_task -- it routes an undelivered failure back "
                    "to 'todo', then call dispatch_task again"
                ),
            }
        if status == "todo":
            return {
                "state": status,
                "why": "task has not been dispatched yet",
                "next": "call dispatch_task to start execution",
            }
        if status == "dispatched":
            return {
                "state": status,
                "why": "an executor run is expected to be in flight, or finished without attach_result being called yet",
                "next": "wait for the run, then call attach_result with the commit/result_ref",
            }
        if status == "awaiting-review":
            return {
                "state": status,
                "why": f"result_ref={result_ref!r} is attached and waiting for a reviewer to be assigned",
                "next": "call request_review to assign a reviewer and start the review run",
            }
        if status == "in-review":
            return {
                "state": status,
                "why": "a review run is expected to be in flight, or finished without record_verdict being called yet",
                "next": "wait for the review run, then call record_verdict with the verdict",
            }
        if status == "changes-requested":
            return {
                "state": status,
                "why": "the reviewer recorded a 'changes' verdict on the last round",
                "next": "call dispatch_task to start a new round of execution",
            }
        if status == "done":
            return {
                "state": status,
                "why": "task already has an approved pass verdict",
                "next": "call land_task to land it (idempotent if already landed)",
            }
        if status == "cancelled":
            return {
                "state": status,
                "why": "task was cancelled and is terminal",
                "next": "create_task a new task if the work is still needed",
            }
        return {
            "state": status,
            "why": "status is not one this helper recognizes",
            "next": "call get_status for the full task record",
        }

    def available_actions(self, task: Task) -> dict[str, Any]:
        """Which tools are valid from this task's current status, and why --
        and for the blocked ones, why and how to unblock them.

        Derived by calling the real FSM gates (``assert_status`` and
        ``TaskStateMachine.require_approved_pass_verdict``) that the actual
        transition methods use, not a hand-maintained status table -- the
        exact drift a table like that produced before (see module docstring
        history on CTV2-1382/CTV2-1388/CTV2-1389). A test asserts that every
        tool this reports as available really can be called, from every
        status, without raising a state-conflict.
        """
        from app.services.task_state_machine import TaskStateMachine

        available: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        def check(tool: str, expected_status: str, fix: str) -> None:
            try:
                self.assert_status(task, expected_status)
            except TransitionConflictError:
                blocked.append(
                    {
                        "tool": tool,
                        "reason": (
                            f"{tool} requires status {expected_status!r}, "
                            f"task is {task.status!r}"
                        ),
                        "fix": fix,
                    }
                )
            else:
                available.append(
                    {
                        "tool": tool,
                        "reason": f"task status is {task.status!r}, which {tool} accepts",
                    }
                )

        # dispatch_task (kind=execute) accepts 'todo', and also re-accepts a
        # task already sitting in 'failed'/'changes-requested' -- this mirrors
        # TaskStateMachine.request_dispatch's own expected_status default.
        dispatch_expected = (
            task.status if task.status in {"failed", "changes-requested"} else "todo"
        )
        check(
            "dispatch_task",
            dispatch_expected,
            "get the task to 'todo', 'failed' or 'changes-requested' first "
            "(reopen_task or record_verdict can move it there)",
        )
        check(
            "attach_result",
            "dispatched",
            "call dispatch_task and let the executor run finish first",
        )
        check(
            "request_review",
            "awaiting-review",
            "call attach_result to deliver a result_ref first",
        )
        check(
            "record_verdict",
            "in-review",
            "call request_review to start a review run first",
        )

        try:
            self.assert_status(task, "failed")
        except TransitionConflictError:
            blocked.append(
                {
                    "tool": "reopen_task",
                    "reason": (
                        f"reopen_task only applies to 'failed' tasks, task is {task.status!r}"
                    ),
                    "fix": "no action needed -- reopen_task is only for stuck 'failed' tasks",
                }
            )
        else:
            available.append(
                {
                    "tool": "reopen_task",
                    "reason": (
                        "task is 'failed'; reopen_task routes it to 'awaiting-review' "
                        "if a result was delivered, otherwise 'todo'"
                    ),
                }
            )

        state_machine = TaskStateMachine(self.db)
        try:
            state_machine.require_approved_pass_verdict(task)
        except PrerequisiteError as exc:
            blocked.append(
                {
                    "tool": "land_task",
                    "reason": str(exc),
                    "fix": "get an approved 'pass' verdict via record_verdict before land_task",
                }
            )
        else:
            available.append(
                {
                    "tool": "land_task",
                    "reason": "task has an approved pass verdict; land_task can merge it",
                }
            )

        return {"available": available, "blocked": blocked}

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
