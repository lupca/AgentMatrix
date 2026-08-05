"""Authoritative task lifecycle transitions and gate decision ledger."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    GateRecord,
    Project,
    Task,
    TaskDependency,
)
from app.services.agent_matcher import AgentMatcher
from app.services.command_builder import build_dispatch_command, build_review_command
from app.services.landing import LandingResult, head_of, land_result
from app.services.outbox import record_run_requested
from app.services.task_event_service import emit_task_event
from app.services.task_state_machine import (
    GateDecision,
    TaskStateMachine,
    TransitionResult,
    _split_result_range,
    update_agent_success_rate,
)
from app.services.task_validators import (
    AutonomyPolicy,
    BrakeDecision,
    BrakeViolationError,
    DependencyCycleError,
    IdempotencyConflictError,
    ModeViolationError,
    OrchestrationError,
    PrerequisiteError,
    StaleIdempotencyRecordError,
    TaskNotFoundError,
    TaskValidator,
    TransitionConflictError,
)

logger = logging.getLogger(__name__)

# Re-export all exception types, dataclasses, and functions for backwards compatibility
__all__ = [
    "OrchestrationError",
    "TaskNotFoundError",
    "TransitionConflictError",
    "ModeViolationError",
    "PrerequisiteError",
    "IdempotencyConflictError",
    "StaleIdempotencyRecordError",
    "BrakeViolationError",
    "DependencyCycleError",
    "BrakeDecision",
    "AutonomyPolicy",
    "GateDecision",
    "TransitionResult",
    "TaskOrchestrationService",
    "build_dispatch_command",
    "build_review_command",
    "record_run_requested",
    "emit_task_event",
    "head_of",
    "land_result",
    "LandingResult",
    "AgentMatcher",
    "update_agent_success_rate",
]


class TaskOrchestrationService:
    """The only application service allowed to mutate task lifecycle fields."""

    MODES = TaskValidator.MODES
    GATED_ACTIONS = {"spec_plan", "dispatch", "review_order", "verdict"}
    PATCHABLE_FIELDS = {"plan", "acceptance_criteria", "priority", "tags", "raw_input"}
    _DEAD_RUN_STATUSES = TaskValidator._DEAD_RUN_STATUSES
    _UNAVAILABLE_REVIEWER_STATUSES = TaskValidator._UNAVAILABLE_REVIEWER_STATUSES

    AutonomyPolicy = AutonomyPolicy

    def __init__(self, db: Session):
        self.db = db
        self.validator = TaskValidator(db)
        self.state_machine = TaskStateMachine(db)

    # Autonomy & mode helpers
    def resolve_autonomy(self, project: Project | str | None) -> AutonomyPolicy:
        return self.validator.resolve_autonomy(project)

    def mode_for_task(self, task: Task, *, risk: str | None = None) -> str:
        return self.validator.mode_for_task(task, risk=risk)

    @property
    def autonomy_enabled(self) -> bool:
        return self.validator.autonomy_enabled

    @property
    def max_cost_usd_per_task(self) -> Decimal:
        return self.validator.max_cost_usd_per_task

    @property
    def max_tokens_per_task(self) -> int:
        return self.validator.max_tokens_per_task

    @property
    def max_concurrent_runs(self) -> int:
        return self.validator.max_concurrent_runs

    @property
    def run_timeout_seconds(self) -> int:
        return self.validator.run_timeout_seconds

    @property
    def max_active_seconds_per_run(self) -> int:
        return self.validator.max_active_seconds_per_run

    @property
    def max_tool_calls_per_run(self) -> int:
        return self.validator.max_tool_calls_per_run

    @property
    def max_no_progress_seconds(self) -> int:
        return self.validator.max_no_progress_seconds

    def check_brakes(
        self,
        task: Task,
        *,
        for_spawn: bool = False,
        audit: bool = False,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> BrakeDecision:
        return self.validator.check_brakes(
            task, for_spawn=for_spawn, audit=audit, run_id=run_id, agent_id=agent_id
        )

    # Transition entry point
    def transition(self, action: str, **kwargs: Any) -> TransitionResult:
        """Generic entry point used by adapters that select an action dynamically."""
        return self.state_machine.transition(action, **kwargs)

    def request_dispatch(
        self,
        *,
        task_id: str,
        agent_id: str,
        actor: str,
        idempotency_key: str,
        timeout_seconds: int | None = None,
        kind: str = "execute",
        expected_status: str | None = None,
        effort: str | None = None,
    ) -> TransitionResult:
        return self.state_machine.request_dispatch(
            task_id=task_id,
            agent_id=agent_id,
            actor=actor,
            idempotency_key=idempotency_key,
            timeout_seconds=timeout_seconds,
            kind=kind,
            expected_status=expected_status,
            effort=effort,
        )

    def request_review(
        self,
        *,
        task_id: str,
        reviewer: str,
        actor: str,
        idempotency_key: str,
        selection_reason: str | None = None,
        timeout_seconds: int | None = None,
        expected_status: str = "awaiting-review",
    ) -> TransitionResult:
        return self.state_machine.request_review(
            task_id=task_id,
            reviewer=reviewer,
            actor=actor,
            idempotency_key=idempotency_key,
            selection_reason=selection_reason,
            timeout_seconds=timeout_seconds,
            expected_status=expected_status,
        )

    def request_verdict(
        self,
        *,
        task_id: str,
        verdict: str,
        ac_results: Any,
        actor: str,
        idempotency_key: str,
        findings: list[Any] | None = None,
        expected_status: str = "in-review",
    ) -> TransitionResult:
        return self.state_machine.request_verdict(
            task_id=task_id,
            verdict=verdict,
            ac_results=ac_results,
            actor=actor,
            idempotency_key=idempotency_key,
            findings=findings,
            expected_status=expected_status,
        )

    def update_agent_success_rate(self, agent_id: str, outcome: float) -> float | None:
        return update_agent_success_rate(self.db, agent_id, outcome)

    def decide_gate(
        self,
        *,
        gate_record_id: int,
        decision: GateDecision,
        actor: str,
        idempotency_key: str,
    ) -> TransitionResult:
        return self.state_machine.decide_gate(
            gate_record_id=gate_record_id,
            decision=decision,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def record_execution_success(
        self,
        *,
        task_id: str,
        result_ref: str | None,
        actor: str,
        idempotency_key: str,
        expected_status: str = "dispatched",
        run_id: str | None = None,
    ) -> TransitionResult:
        return self.state_machine.record_execution_success(
            task_id=task_id,
            result_ref=result_ref,
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            run_id=run_id,
        )

    def record_execution_failure(
        self,
        *,
        task_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "dispatched",
        run_id: str | None = None,
        error_code: str | None = None,
    ) -> TransitionResult:
        return self.state_machine.record_execution_failure(
            task_id=task_id,
            error=error,
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            run_id=run_id,
            error_code=error_code,
        )

    def record_review_failure(
        self,
        *,
        task_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "in-review",
        run_id: str | None = None,
        error_details: dict[str, Any] | None = None,
    ) -> TransitionResult:
        return self.state_machine.record_review_failure(
            task_id=task_id,
            error=error,
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            run_id=run_id,
            error_details=error_details,
        )

    def escalate_task(
        self, *, task_id: str, reason: str, actor: str = "system"
    ) -> GateRecord:
        return self.state_machine.escalate_task(
            task_id=task_id, reason=reason, actor=actor
        )

    def reopen_failed_task(self, *, task_id: str, actor: str):
        return self.state_machine.reopen_failed_task(task_id=task_id, actor=actor)

    def record_dispatch_queue_failure(
        self,
        *,
        run_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
    ) -> TransitionResult:
        return self.state_machine.record_dispatch_queue_failure(
            run_id=run_id, error=error, actor=actor, idempotency_key=idempotency_key
        )

    def cancel_run(
        self,
        *,
        run_id: str,
        actor: str,
        idempotency_key: str,
        reason: str = "Cancelled by user",
    ) -> TransitionResult:
        return self.state_machine.cancel_run(
            run_id=run_id,
            actor=actor,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    def attach_result(
        self,
        *,
        task_id: str,
        commit: str,
        option: str = "request_review",
        actor: str = "system",
        idempotency_key: str | None = None,
    ) -> TransitionResult:
        return self.state_machine.attach_result(
            task_id=task_id,
            commit=commit,
            option=option,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    def update_task_fields(
        self,
        *,
        task_id: str,
        patch: dict[str, Any],
        actor: str,
    ) -> Task:
        return self.state_machine.update_task_fields(
            task_id=task_id, patch=patch, actor=actor
        )

    def write_spec_plan(
        self,
        *,
        task_id: str,
        actor: str,
        acceptance_criteria: list[str],
        constraints: list[str],
        evidence: list[dict[str, Any]],
        prior_art: list[str],
        ruled_out: list[dict[str, Any]],
        limits: dict[str, Any] | None,
        plan: str,
        files: list[str],
        tests: list[str],
        risk: str,
        flows: list[str],
        spec_clarity: str,
        open_questions: list[str],
        planner: str,
        critic: str | None = None,
        critic_verdict: str | None = None,
        critic_findings: list[dict[str, Any]] | None = None,
        critic_summary: str | None = None,
        critic_tokens: int | None = None,
    ) -> Task:
        return self.state_machine.write_spec_plan(
            task_id=task_id,
            actor=actor,
            acceptance_criteria=acceptance_criteria,
            constraints=constraints,
            evidence=evidence,
            prior_art=prior_art,
            ruled_out=ruled_out,
            limits=limits,
            plan=plan,
            files=files,
            tests=tests,
            risk=risk,
            flows=flows,
            spec_clarity=spec_clarity,
            open_questions=open_questions,
            planner=planner,
            critic=critic,
            critic_verdict=critic_verdict,
            critic_findings=critic_findings,
            critic_summary=critic_summary,
            critic_tokens=critic_tokens,
        )

    def record_plan_critic_verdict(
        self,
        *,
        task_id: str,
        actor: str,
        critic: str,
        verdict: str,
        findings: list[dict[str, Any]],
        summary: str,
        tokens: int,
    ) -> Task:
        return self.state_machine.record_plan_critic_verdict(
            task_id=task_id,
            actor=actor,
            critic=critic,
            verdict=verdict,
            findings=findings,
            summary=summary,
            tokens=tokens,
        )

    def reopen_for_replan(
        self,
        *,
        task_id: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "changes-requested",
    ) -> TransitionResult:
        return self.state_machine.reopen_for_replan(
            task_id=task_id,
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
        )

    def changes_round_count(self, task_id: str) -> int:
        return self.state_machine.changes_round_count(task_id)

    def review_gate_count(self, task_id: str, *, round_: int) -> int:
        """How many review_order requests this task already made this round.

        The orchestration driver uses this to give each retry its own
        idempotency key -- see the comment at its call site.
        """
        return self.state_machine.review_gate_count(task_id, round_=round_)

    def add_dependency(
        self,
        *,
        task_id: str,
        depends_on_task_id: str,
        actor: str,
    ) -> TaskDependency:
        return self.state_machine.add_dependency(
            task_id=task_id, depends_on_task_id=depends_on_task_id, actor=actor
        )

    def unmet_dependencies(self, task_id: str) -> list[Task]:
        return self.validator.unmet_dependencies(task_id)

    def failed_dependencies(self, task_id: str) -> list[str]:
        return self.validator.failed_dependencies(task_id)

    def dependent_task_ids(self, task_id: str) -> list[str]:
        return self.validator.dependent_task_ids(task_id)

    def wake_dependents(self, task_id: str) -> None:
        self.state_machine.wake_dependents(task_id)

    def complete_no_commit_task(
        self, *, task_id: str, actor: str, run_id: str | None = None
    ) -> dict:
        return self.state_machine.complete_no_commit_task(
            task_id=task_id, actor=actor, run_id=run_id
        )

    def land_task(self, *, task_id: str, actor: str) -> dict:
        return self.state_machine.land_task(task_id=task_id, actor=actor)

    # Internal helper methods mapped for compatibility with tests / callers
    def _task(self, task_id: str) -> Task:
        return self.validator.task(task_id)

    def _cas_status(self, task: Task, new_status: str) -> None:
        self.state_machine.cas_status(task, new_status)

    def _assert_status(self, task: Task, expected_status: str) -> None:
        self.validator.assert_status(task, expected_status)

    def _input_hash(self, payload: dict[str, Any]) -> str:
        return TaskValidator.input_hash(payload)

    def _require_independent(self, executor: str | None, reviewer: str | None) -> None:
        self.validator.require_independent(executor, reviewer)

    def _principal(self, value: str) -> str:
        return TaskValidator.principal(value)

    def _ledger_record(self, **kwargs: Any) -> GateRecord:
        return self.state_machine.ledger_record(**kwargs)

    def _audit(self, task: Task, record: GateRecord, reason: str | None = None) -> None:
        self.state_machine.audit(task, record, reason=reason)

    def _idempotent_record(self, task_id: str, idempotency_key: str, input_hash: str) -> GateRecord | None:
        return self.validator.idempotent_record(task_id, idempotency_key, input_hash)

    def _reject_if_stale_dispatch_record(self, record: GateRecord) -> None:
        self.validator.reject_if_stale_dispatch_record(record)

    def _result_for_record(self, task: Task, record: GateRecord) -> TransitionResult:
        return self.state_machine.result_for_record(task, record)

    def _request_gate(self, **kwargs: Any) -> TransitionResult:
        return self.state_machine.request_gate(**kwargs)

    def _apply_gate(self, **kwargs: Any) -> tuple[Any, str | None]:
        return self.state_machine.apply_gate(**kwargs)

    def _validate_verdict_prerequisites(self, task: Task, *, actor: str, verdict: str, ac_results: Any) -> None:
        self.validator.validate_verdict_prerequisites(task, actor=actor, verdict=verdict, ac_results=ac_results)

    def _validate_common(self, task: Task, actor: str, idempotency_key: str) -> None:
        TaskValidator.validate_common(task, actor, idempotency_key)

    def _evaluation_results(self, ac_results: Any) -> list[bool]:
        return TaskValidator.evaluation_results(ac_results)
