"""Authoritative task lifecycle transitions and gate decision ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Agent,
    AgentRun,
    AuditLog,
    GateRecord,
    LLMUsage,
    Project,
    Setting,
    Task,
    TaskDependency,
    TaskRound,
)
from app.db.models import Session as SessionModel
from app.services.command_builder import build_dispatch_command, build_review_command
from app.services.task_event_service import emit_task_event

logger = logging.getLogger(__name__)

GateDecision = Literal["approved", "rejected"]


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
    """A cached gate record refers to a run that is no longer active.

    Returning this record's cached ``applied=True`` result would tell the
    caller a dispatch is in flight when in fact nothing is running. Callers
    must retry with a new idempotency key rather than reusing the stale one.
    """


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


def _split_result_range(result_ref: str) -> tuple[str | None, str | None]:
    """Expose the committed review range to the review gate/run context."""
    if ".." not in result_ref:
        return None, result_ref
    base, head = result_ref.split("..", 1)
    return base or None, head or None


@dataclass(frozen=True)
class TransitionResult:
    task: Task
    gate_record: GateRecord
    applied: bool
    agent_run: AgentRun | None = None
    context: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return self.gate_record.status


class TaskOrchestrationService:
    """The only application service allowed to mutate task lifecycle fields."""

    MODES = {"supervised", "plan-only", "bypass"}
    GATED_ACTIONS = {"spec_plan", "dispatch", "review_order", "verdict"}
    PATCHABLE_FIELDS = {"plan", "acceptance_criteria", "priority", "tags"}
    # A run in any of these statuses is unconditionally no longer "in flight".
    # "failed" is deliberately excluded here: the worker retries a failed run
    # in place (status goes back to "queued") until its attempts are
    # exhausted, so a persisted "failed" status only means dead once
    # ``attempt >= max_attempts`` — see `_reject_if_stale_dispatch_record`.
    _DEAD_RUN_STATUSES = {"success", "timeout", "cancelled"}

    @dataclass(frozen=True)
    class AutonomyPolicy:
        autonomy: str = "supervised"
        auto_max_risk: str = "normal"
        auto_max_rounds: int = 3

    # Unknown risk labels, including legacy labels such as ``medium``, are
    # deliberately ineligible for automatic execution (fail safe).
    _RISK_LEVELS = {"low": 0, "normal": 1, "high": 2}
    _AUTONOMY_VALUES = {"plan-only", "supervised", "auto"}

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
        return self.AutonomyPolicy(autonomy, max_risk, max_rounds)

    def mode_for_task(self, task: Task, *, risk: str | None = None) -> str:
        # Non-default modes are explicit per-task governance choices. Keep
        # them stable while allowing the default supervised snapshot to be
        # re-resolved when the project/global autonomy policy changes.
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

    def __init__(self, db: Session):
        self.db = db

    def transition(self, action: str, **kwargs: Any) -> TransitionResult:
        """Generic entry point used by adapters that select an action dynamically."""
        handlers = {
            "dispatch": self.request_dispatch,
            "review_order": self.request_review,
            "verdict": self.request_verdict,
            "execution_succeeded": self.record_execution_success,
            "execution_failed": self.record_execution_failure,
            "cancel": self.cancel_run,
            "decide": self.decide_gate,
        }
        try:
            handler = handlers[action]
        except KeyError as exc:
            raise OrchestrationError(f"Unknown transition action: {action}") from exc
        return handler(**kwargs)

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
        if kind not in {"execute", "review"}:
            raise PrerequisiteError(f"Invalid dispatch kind: {kind}")
        task = self._task(task_id)
        if expected_status is None:
            if kind == "review":
                expected_status = "awaiting-review"
            elif task.status == "failed":
                expected_status = "failed"  # Allow retry of failed tasks
            else:
                expected_status = "todo"
        elif kind == "execute" and expected_status not in {"todo", "failed"}:
            # Execute dispatch accepts "todo" (normal) or "failed" (retry).
            # Review dispatch may target other pre-states like "awaiting-review".
            raise PrerequisiteError(
                "execute dispatch requires expected_status='todo' or 'failed'"
            )
        if not (task.acceptance_criteria or []) and not task.legacy_no_ac:
            raise PrerequisiteError(
                "dispatch requires acceptance_criteria; run the spec/plan gate "
                "first (or set legacy_no_ac for pre-existing tasks)"
            )
        agent = self.db.get(Agent, agent_id)
        if agent is None:
            raise PrerequisiteError(f"Agent {agent_id} not found")
        if kind == "review":
            self._require_independent(task.executor, agent_id)
        project = self.db.get(Project, task.project)
        resolved_effort = effort or agent.effort or "medium"
        try:
            command, repo_root, cli = build_dispatch_command(
                task, agent, project, effort=resolved_effort
            )
        except ValueError as exc:
            raise PrerequisiteError(str(exc)) from exc

        return self._request_gate(
            task=task,
            gate_type="dispatch",
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            payload={
                "agent_id": agent_id,
                "command": command,
                "repo_root": repo_root,
                "cli": cli,
                "timeout_seconds": timeout_seconds or self.run_timeout_seconds,
                "kind": kind,
                "agent_role": "reviewer" if kind == "review" else "executor",
                "effort": resolved_effort,
            },
        )

    def request_review(
        self,
        *,
        task_id: str,
        reviewer: str,
        actor: str,
        idempotency_key: str,
        timeout_seconds: int | None = None,
        expected_status: str = "awaiting-review",
    ) -> TransitionResult:
        task = self._task(task_id)
        if not task.result_ref or not task.result_ref.strip():
            raise PrerequisiteError("result_ref is required before review")
        if not reviewer or not reviewer.strip():
            raise PrerequisiteError("reviewer is required")
        self._require_independent(task.executor, reviewer)
        base_ref, head_ref = _split_result_range(task.result_ref)
        if not base_ref or not head_ref:
            raise PrerequisiteError(
                "result_ref must be a recorded base..head range before review "
                "(the review boundary is never inferred)"
            )
        agent = self.db.get(Agent, reviewer)
        if agent is None:
            raise PrerequisiteError(f"Agent {reviewer} not found")
        project = self.db.get(Project, task.project)
        try:
            command, repo_root, cli = build_review_command(
                task, agent, project, base_ref, head_ref
            )
        except ValueError as exc:
            raise PrerequisiteError(str(exc)) from exc

        return self._request_gate(
            task=task,
            gate_type="review_order",
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            payload={
                "reviewer": reviewer,
                "result_ref": task.result_ref,
                "base_ref": base_ref,
                "head_ref": head_ref,
                "command": command,
                "repo_root": repo_root,
                "cli": cli,
                "timeout_seconds": timeout_seconds or self.run_timeout_seconds,
            },
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
        task = self._task(task_id)
        normalized_verdict = verdict.strip().lower()
        if normalized_verdict not in {"pass", "changes"}:
            raise PrerequisiteError("Verdict must be pass or changes")
        self._validate_verdict_prerequisites(
            task,
            actor=actor,
            verdict=normalized_verdict,
            ac_results=ac_results,
        )
        return self._request_gate(
            task=task,
            gate_type="verdict",
            actor=actor,
            idempotency_key=idempotency_key,
            expected_status=expected_status,
            payload={
                "verdict": normalized_verdict,
                "ac_results": ac_results,
                "findings": findings or [],
                "result_ref": task.result_ref,
                "reviewer": task.reviewer,
            },
        )

    def decide_gate(
        self,
        *,
        gate_record_id: int,
        decision: GateDecision,
        actor: str,
        idempotency_key: str,
    ) -> TransitionResult:
        if decision not in {"approved", "rejected"}:
            raise PrerequisiteError("Decision must be approved or rejected")
        pending = (
            self.db.query(GateRecord)
            .filter(GateRecord.id == gate_record_id)
            .with_for_update()
            .first()
        )
        if pending is None or pending.status != "pending":
            raise TransitionConflictError(
                f"Pending gate record {gate_record_id} not found"
            )
        task = self._task(pending.task_id)
        decision_payload = {
            "pending_id": pending.id,
            "decision": decision,
            "gate_type": pending.gate_type,
        }
        input_hash = self._input_hash(decision_payload)
        existing = self._idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            # No staleness guard here (unlike `_request_gate`): this key is
            # keyed off the decision itself (gate_record_id + decision), not
            # off a dispatch attempt, so it has no attempt/nonce component to
            # roll forward. The cached record is an immutable historical
            # fact — "this decision was made and it created run X" — which
            # stays true and safely replayable no matter how run X later
            # finishes. Rejecting it as "stale" here previously turned any
            # replay of an already-decided gate (e.g. a duplicate
            # POST /gates/{id}/decision) into a permanent, unrecoverable
            # error once the created run went terminal (CTV2-088 round 2).
            return self._result_for_record(task, existing)

        prior_decision = (
            self.db.query(GateRecord)
            .filter(
                GateRecord.parent_id == pending.id,
                GateRecord.status.in_(["approved", "rejected"]),
            )
            .first()
        )
        if prior_decision is not None:
            raise TransitionConflictError(
                f"Gate record {pending.id} was already {prior_decision.status}"
            )

        effective_decision = decision
        reason: str | None = None
        if task.mode == "plan-only" and pending.gate_type in {"dispatch", "verdict"}:
            effective_decision = "rejected"
            reason = "plan-only mode blocks this transition"

        run: AgentRun | None = None
        output_ref: str | None = None
        if effective_decision == "approved":
            run, output_ref = self._apply_gate(
                task,
                pending.gate_type,
                pending.input_payload or {},
            )

        record = self._ledger_record(
            task=task,
            gate_type=pending.gate_type,
            status=effective_decision,
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=decision_payload,
            output_ref=output_ref,
            output_payload=self._gate_output(task, pending.gate_type),
            error_message=reason,
            parent_id=pending.id,
        )
        task.awaiting_approval = False
        task.approval_prompt = None
        self._audit(task, record, reason=reason)
        emit_task_event(
            task_id=task.id,
            event_type="gate_passed" if effective_decision == "approved" else "gate_rejected",
            payload={
                "gate": pending.gate_type,
                "gate_record_id": record.id,
                "reason": reason,
            },
            db=self.db,
        )
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        self._resolve_gate_notification(task.id, pending.gate_type, pending.id, effective_decision)
        if run is not None:
            self.db.refresh(run)
        if pending.gate_type == "verdict" and task.status == "done":
            self.wake_dependents(task.id)
        return TransitionResult(
            task=task,
            gate_record=record,
            applied=effective_decision == "approved",
            agent_run=run,
            context=(pending.input_payload or {}) if run is not None else None,
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
        task = self._task(task_id)
        payload = {
            "expected_status": expected_status,
            "result_ref": result_ref,
            "run_id": run_id,
        }
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        now = datetime.now(timezone.utc)
        task.status = "awaiting-review"
        task.current_gate = "review_order"
        task.result_ref = result_ref
        task.error = None
        task.updated_at = now
        record = self._ledger_record(
            task=task,
            gate_type="execution",
            status="approved",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=result_ref or run_id,
            output_payload={"status": "awaiting-review", "run_id": run_id},
        )
        self._audit(task, record)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        return TransitionResult(task, record, True)

    def record_execution_failure(
        self,
        *,
        task_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "dispatched",
        run_id: str | None = None,
    ) -> TransitionResult:
        task = self._task(task_id)
        payload = {
            "expected_status": expected_status,
            "error": error,
            "run_id": run_id,
        }
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        task.status = "failed"
        task.error = error
        task.updated_at = datetime.now(timezone.utc)
        record = self._ledger_record(
            task=task,
            gate_type="execution",
            status="rejected",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=run_id,
            error_message=error,
        )
        self._audit(task, record, reason=error)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        self.wake_dependents(task_id)
        return TransitionResult(task, record, True)

    def record_review_failure(
        self,
        *,
        task_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "in-review",
        run_id: str | None = None,
    ) -> TransitionResult:
        """Escalate a review run that produced no usable, schema-valid result.

        A missing or malformed review artifact must never be treated as an
        implicit pass — it is routed to the same human-escalation shape as a
        safety-brake trip (``status="failed"`` + ``awaiting_approval``)
        rather than left stuck in ``in-review`` or silently advanced.
        """
        task = self._task(task_id)
        payload = {
            "expected_status": expected_status,
            "error": error,
            "run_id": run_id,
        }
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        now = datetime.now(timezone.utc)
        task.status = "failed"
        task.error = error
        task.awaiting_approval = True
        task.approval_prompt = f"Review result invalid or missing: {error}"
        task.updated_at = now
        record = self._ledger_record(
            task=task,
            gate_type="review_result",
            status="rejected",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=run_id,
            error_message=error,
        )
        self._audit(task, record, reason=error)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        self.wake_dependents(task_id)
        return TransitionResult(task, record, True)

    def record_dispatch_queue_failure(
        self,
        *,
        run_id: str,
        error: str,
        actor: str,
        idempotency_key: str,
    ) -> TransitionResult:
        run = self.db.get(AgentRun, run_id)
        if run is None:
            raise TransitionConflictError(f"Run {run_id} not found")
        task = self._task(run.task_id)
        # A review-kind run reaches this from "in-review" (set by the
        # review_order gate), not "dispatched" like an execute run; failure
        # rolls the task back to "awaiting-review" so review can be
        # re-requested, rather than all the way to "todo".
        expected_status = "in-review" if run.kind == "review" else "dispatched"
        reset_status = "awaiting-review" if run.kind == "review" else "todo"
        payload = {"run_id": run_id, "error": error}
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        now = datetime.now(timezone.utc)
        run.status = "failed"
        run.error_message = error
        run.completed_at = now
        task.status = reset_status
        task.error = error
        task.updated_at = now
        record = self._ledger_record(
            task=task,
            gate_type="dispatch_queue",
            status="rejected",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=run_id,
            error_message=error,
        )
        self._audit(task, record, reason=error)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        return TransitionResult(task, record, True, agent_run=run)

    def cancel_run(
        self,
        *,
        run_id: str,
        actor: str,
        idempotency_key: str,
    ) -> TransitionResult:
        run = self.db.get(AgentRun, run_id)
        if run is None:
            raise TransitionConflictError(f"Run {run_id} not found")
        task = self._task(run.task_id)
        payload = {"run_id": run_id}
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        if run.status not in {"queued", "running"}:
            raise TransitionConflictError(
                f"Cannot cancel run in status: {run.status}"
            )
        now = datetime.now(timezone.utc)
        run.status = "cancelled"
        run.error_message = "Cancelled by user"
        run.completed_at = now
        if task.status == "dispatched":
            task.status = "todo"
        task.updated_at = now
        record = self._ledger_record(
            task=task,
            gate_type="cancellation",
            status="approved",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=run_id,
            output_payload={"run_status": "cancelled", "task_status": task.status},
        )
        self._audit(task, record)
        emit_task_event(
            task_id=task.id,
            event_type="cancelled",
            payload={
                "run_id": run.id,
                "cancelled_by": actor,
            },
            db=self.db,
        )
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        self.db.refresh(run)
        return TransitionResult(task, record, True, agent_run=run)

    def update_task_fields(
        self,
        *,
        task_id: str,
        patch: dict[str, Any],
        actor: str,
    ) -> Task:
        """Edit plan/acceptance_criteria/priority/tags without touching status.

        Status transitions stay exclusive to the gate flow (dispatch/review/
        verdict); this is metadata-only and always writes an AuditLog row.
        """
        task = self._task(task_id)
        if not actor or not actor.strip():
            raise PrerequisiteError("actor is required")
        if not patch:
            raise PrerequisiteError("patch must include at least one field")
        unknown = set(patch) - self.PATCHABLE_FIELDS
        if unknown:
            raise PrerequisiteError(
                f"Cannot patch fields: {', '.join(sorted(unknown))}. "
                f"Allowed fields: {', '.join(sorted(self.PATCHABLE_FIELDS))}"
            )

        for field, value in patch.items():
            setattr(task, field, value)
        task.updated_at = datetime.now(timezone.utc)
        self.db.add(
            AuditLog(
                task_id=task.id,
                action="update_task",
                actor=actor,
                details={"patch": patch},
            )
        )
        self.db.commit()
        self.db.refresh(task)
        return task

    def write_spec_plan(
        self,
        *,
        task_id: str,
        actor: str,
        acceptance_criteria: list[str],
        plan: str,
        files: list[str],
        tests: list[str],
        risk: str,
        flows: list[str],
    ) -> Task:
        """Persist the spec/plan gate's LLM output onto a task ('todo' only).

        This is the sole write path for a task's AC/plan/files/tests/risk on
        the way into dispatch: `request_dispatch` refuses tasks with empty
        `acceptance_criteria`, so this call (or an explicit `legacy_no_ac`
        backfill) is what actually opens the dispatch gate for a new task.
        """
        task = self._task(task_id)
        if not actor or not actor.strip():
            raise PrerequisiteError("actor is required")
        self._assert_status(task, "todo")
        if not acceptance_criteria:
            raise PrerequisiteError("acceptance_criteria must not be empty")

        task.acceptance_criteria = acceptance_criteria
        task.plan = plan
        task.files = files
        task.tests = tests
        task.risk = risk
        # Existing explicitly governed tasks (notably legacy bypass tasks)
        # retain their mode; newly created tasks start supervised and are
        # resolved from the autonomy policy when their risk is known.
        if task.mode == "supervised":
            task.mode = self.mode_for_task(task, risk=risk)
        task.flows = flows
        task.current_gate = "plan"
        task.updated_at = datetime.now(timezone.utc)
        self.db.add(
            AuditLog(
                task_id=task.id,
                action="spec_plan_generated",
                actor=actor,
                details={
                    "ac_count": len(acceptance_criteria),
                    "files": files,
                    "flows": flows,
                    "risk": risk,
                },
            )
        )
        self.db.commit()
        self.db.refresh(task)
        return task

    def reopen_for_replan(
        self,
        *,
        task_id: str,
        actor: str,
        idempotency_key: str,
        expected_status: str = "changes-requested",
    ) -> TransitionResult:
        """Reopen a changes-requested task back to `todo` for another round.

        Mechanical only: it does not regenerate acceptance_criteria/plan
        content. A real content replan is a separate, explicit call into
        `write_spec_plan` before the task is re-dispatched; this method just
        clears the terminal `changes-requested` state so `request_dispatch`
        (which requires status "todo") can run again.
        """
        task = self._task(task_id)
        payload = {"expected_status": expected_status}
        input_hash = self._input_hash(payload)
        existing = self._idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        task.status = "todo"
        task.current_gate = "plan"
        task.verdict = None
        task.updated_at = datetime.now(timezone.utc)
        record = self._ledger_record(
            task=task,
            gate_type="replan",
            status="approved",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_payload={"task_status": task.status},
        )
        self._audit(task, record)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        return TransitionResult(task, record, True)

    def changes_round_count(self, task_id: str) -> int:
        """Return the highest persisted TaskRound number for a task.

        Used as the round counter by the orchestration driver
        (`advance_task`). TaskRound is the source of truth for dispatch
        history; GateRecord also contains replan records, but those records
        do not represent dispatch rounds and may exist for legacy tasks.
        """
        return int(
            self.db.query(func.max(TaskRound.round_no))
            .filter(TaskRound.task_id == task_id)
            .scalar()
            or 0
        )

    def add_dependency(
        self,
        *,
        task_id: str,
        depends_on_task_id: str,
        actor: str,
    ) -> TaskDependency:
        """Record that ``task_id`` cannot dispatch until ``depends_on_task_id`` is done.

        Rejects a self-dependency and any edge that would close a cycle in
        the existing dependency graph (checked by DFS before the row is
        written) -- both are ``DependencyCycleError`` so callers can treat
        them identically.
        """
        if task_id == depends_on_task_id:
            raise DependencyCycleError(f"Task {task_id} cannot depend on itself")
        if self.db.get(Task, task_id) is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        if self.db.get(Task, depends_on_task_id) is None:
            raise TaskNotFoundError(f"Task {depends_on_task_id} not found")

        existing = self.db.get(TaskDependency, (task_id, depends_on_task_id))
        if existing is not None:
            return existing

        if self._creates_cycle(task_id, depends_on_task_id):
            raise DependencyCycleError(
                f"Adding dependency {task_id} -> {depends_on_task_id} would "
                "create a cycle"
            )

        edge = TaskDependency(task_id=task_id, depends_on_task_id=depends_on_task_id)
        self.db.add(edge)
        self.db.add(
            AuditLog(
                task_id=task_id,
                action="add_dependency",
                actor=actor,
                details={"depends_on_task_id": depends_on_task_id},
            )
        )
        self.db.commit()
        self.db.refresh(edge)
        return edge

    def _creates_cycle(self, task_id: str, depends_on_task_id: str) -> bool:
        """Would ``task_id -> depends_on_task_id`` close a cycle?

        True iff ``depends_on_task_id`` can already reach ``task_id`` by
        walking existing ``depends_on`` edges forward -- i.e. a path
        ``depends_on_task_id -> ... -> task_id`` already exists, so adding
        the new edge would complete the loop.
        """
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
        """Dependencies of ``task_id`` that have not reached ``done``."""
        dep_ids = self._dependency_ids(task_id)
        if not dep_ids:
            return []
        return (
            self.db.query(Task)
            .filter(Task.id.in_(dep_ids), Task.status != "done")
            .all()
        )

    def failed_dependencies(self, task_id: str) -> list[str]:
        """Dependency ids of ``task_id`` that are missing or ``failed``.

        A missing/failed dependency can never reach ``done``, so it must
        escalate the dependent task instead of leaving it parked forever.
        """
        dep_ids = self._dependency_ids(task_id)
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

    def _dependency_ids(self, task_id: str) -> list[str]:
        return [
            row.depends_on_task_id
            for row in self.db.query(TaskDependency.depends_on_task_id)
            .filter(TaskDependency.task_id == task_id)
            .all()
        ]

    def dependent_task_ids(self, task_id: str) -> list[str]:
        """Ids of every task whose dependency graph includes ``task_id``."""
        return [
            row.task_id
            for row in self.db.query(TaskDependency.task_id)
            .filter(TaskDependency.depends_on_task_id == task_id)
            .all()
        ]

    def wake_dependents(self, task_id: str) -> None:
        """Nudge the orchestration driver for every task waiting on ``task_id``.

        Called after ``task_id`` reaches a terminal status (``done`` or
        ``failed``) so a dependent parked on it is not left waiting until
        some unrelated trigger happens to re-check it. Deferred import
        avoids a module cycle (``agent_runner`` imports this service at
        module scope); safe to call unconditionally even when ``task_id``
        has no dependents.
        """
        from app.workers.agent_runner import advance_task

        for dependent_id in self.dependent_task_ids(task_id):
            try:
                advance_task.send(dependent_id, "dependency_closed")
            except Exception:
                logger.warning(
                    "Could not enqueue advance_task for dependent %s of %s",
                    dependent_id,
                    task_id,
                    exc_info=True,
                )

    def _request_gate(
        self,
        *,
        task: Task,
        gate_type: str,
        actor: str,
        idempotency_key: str,
        expected_status: str,
        payload: dict[str, Any],
    ) -> TransitionResult:
        self._validate_common(task, actor, idempotency_key)
        if gate_type == "dispatch":
            decision = self.check_brakes(task, for_spawn=True, audit=True)
            if not decision.allowed:
                if decision.queue:
                    # A queued AgentRun is the durable queue representation;
                    # the worker repeats this check before process creation.
                    pass
                else:
                    raise BrakeViolationError(decision.reason or "Autonomy brake engaged")
        request_payload = {
            **payload,
            "expected_status": expected_status,
            "gate_type": gate_type,
        }
        input_hash = self._input_hash(request_payload)
        existing = self._idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            # Note on CTV2-088 round 2 / AC2 ("status check before returning
            # an idempotent record"): `_assert_status(task, expected_status)`
            # is deliberately NOT run unconditionally ahead of this branch.
            # `expected_status` is the task's *pre*-transition state; once a
            # cached record has already applied (bypass mode, or an approved
            # supervised decision), the task has by design moved past it, so
            # asserting it here would turn every legitimate idempotent replay
            # into a hard error — verified against
            # `test_bypass_dispatch_is_audited_and_idempotent`, which relies
            # on a same-key replay after the first call already advanced
            # task.status. The state check AC2 actually calls for — never
            # returning `applied=True` when the current state doesn't back
            # that claim — is enforced here via
            # `_reject_if_stale_dispatch_record`, which inspects the
            # referenced AgentRun rather than the task's pre-transition
            # status; see its docstring for why that is the correct
            # substitute rather than a literal statement reorder.
            self._reject_if_stale_dispatch_record(existing)
            return self._result_for_record(task, existing)
        self._assert_status(task, expected_status)
        if gate_type == "dispatch":
            active_run = (
                self.db.query(AgentRun)
                .filter(
                    AgentRun.task_id == task.id,
                    AgentRun.status.in_(["queued", "running"]),
                )
                .first()
            )
            if active_run is not None:
                raise TransitionConflictError(
                    f"Task {task.id} already has active run: {active_run.id}"
                )

        effective_mode = self.mode_for_task(task)
        if effective_mode == "plan-only" and gate_type in {"dispatch", "verdict"}:
            record = self._ledger_record(
                task=task,
                gate_type=gate_type,
                status="rejected",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
                error_message="plan-only mode blocks this transition",
            )
            self._audit(task, record, reason=record.error_message)
            self.db.commit()
            raise ModeViolationError(record.error_message)

        if effective_mode == "supervised":
            record = self._ledger_record(
                task=task,
                gate_type=gate_type,
                status="pending",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
            )
            task.awaiting_approval = True
            task.approval_prompt = (
                f"Approve {gate_type} gate for task {task.id} "
                f"(request {idempotency_key})?"
            )
            self._audit(task, record)
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            self._notify_gate_pending(task, record)
            return TransitionResult(task, record, False)

        run, output_ref = self._apply_gate(task, gate_type, request_payload)
        record = self._ledger_record(
            task=task,
            gate_type=gate_type,
            status="approved",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=request_payload,
            output_ref=output_ref,
            output_payload=self._gate_output(task, gate_type),
        )
        self._audit(task, record)
        self.db.commit()
        self.db.refresh(task)
        self.db.refresh(record)
        if run is not None:
            self.db.refresh(run)
        if gate_type == "verdict" and task.status == "done":
            self.wake_dependents(task.id)
        return TransitionResult(
            task,
            record,
            True,
            agent_run=run,
            context=request_payload if run is not None else None,
        )

    def _notify_gate_pending(self, task: Task, record: GateRecord) -> None:
        """Emit a gate_pending event into task_events table (CTV2-115)."""
        emit_task_event(
            task_id=task.id,
            event_type="gate_pending",
            payload={
                "gate": record.gate_type,
                "gate_record_id": record.id,
            },
            db=self.db,
        )

    def _resolve_gate_notification(
        self,
        task_id: str,
        gate_type: str,
        gate_record_id: int,
        state: str,
    ) -> None:
        """Close a pending inbox notice so a later pending state can notify."""
        global_session = (
            self.db.query(SessionModel)
            .filter(
                SessionModel.context_level == "global",
                SessionModel.status == "active",
            )
            .order_by(SessionModel.last_activity_at.desc())
            .first()
        )
        if global_session is None:
            return
        messages = list(global_session.messages or [])
        changed = False
        for message in messages:
            if (
                message.get("kind") == "gate_notification"
                and message.get("task_id") == task_id
                and message.get("gate") == gate_type
                and message.get("gate_record_id") == gate_record_id
                and message.get("notification_state") == "pending"
            ):
                message["notification_state"] = state
                changed = True
        if changed:
            global_session.messages = messages
            global_session.message_count = len(messages)
            global_session.last_activity_at = datetime.now(timezone.utc)
            self.db.commit()

    def _start_round(
        self,
        task: Task,
        *,
        agent_id: str,
        run_id: str,
        now: datetime,
    ) -> TaskRound:
        """Open a new TaskRound for an execute dispatch (CTV2-201).

        One row per execute-dispatch attempt, numbered from the highest
        ``round_no`` seen for this task so far -- history from earlier
        rounds (dispatch/review agents, verdicts) is preserved instead of
        being overwritten the way the flat `Task` columns are.
        """
        next_round_no = (
            self.db.query(func.max(TaskRound.round_no))
            .filter(TaskRound.task_id == task.id)
            .scalar()
            or 0
        ) + 1
        round_id = str(uuid.uuid4())
        task_round = TaskRound(
            id=round_id,
            task_id=task.id,
            round_no=next_round_no,
            status="dispatched",
            executor_agent_id=agent_id,
            executor_run_id=run_id,
            started_at=now,
        )
        self.db.add(task_round)
        # Flush the round's INSERT ahead of the task's UPDATE: tasks and
        # task_rounds reference each other (task_rounds.task_id -> tasks.id,
        # tasks.current_round_id -> task_rounds.id), so without this the
        # unit of work can order the FK-setting UPDATE before the row it
        # points at exists and the database rejects it.
        self.db.flush()
        task.current_round_id = round_id
        return task_round

    def _record_verdict_on_round(
        self,
        task: Task,
        *,
        verdict: str,
        now: datetime,
    ) -> None:
        """Fold a verdict's outcome into the task's current TaskRound.

        A no-op when the task has no current round -- e.g. tests and other
        callers that drive a task straight into `in-review` without going
        through `request_dispatch` first.
        """
        if not task.current_round_id:
            return
        current_round = self.db.get(TaskRound, task.current_round_id)
        if current_round is None:
            return
        review_run = self._terminal_review_run(task.id)
        current_round.verdict = verdict
        current_round.findings_ref = task.findings
        current_round.reviewer_agent_id = task.reviewer
        current_round.reviewer_run_id = review_run.id if review_run else None
        current_round.result_ref = task.result_ref
        current_round.status = task.status
        current_round.completed_at = now

    def _apply_gate(
        self,
        task: Task,
        gate_type: str,
        payload: dict[str, Any],
    ) -> tuple[AgentRun | None, str | None]:
        self._assert_status(task, str(payload["expected_status"]))
        now = datetime.now(timezone.utc)
        if gate_type == "dispatch":
            run_id = str(uuid.uuid4())
            kind = str(payload.get("kind", "execute"))
            agent_role = str(payload.get("agent_role", "executor"))
            if kind == "review":
                self._require_independent(task.executor, str(payload["agent_id"]))
            run = AgentRun(
                id=run_id,
                task_id=task.id,
                agent_id=str(payload["agent_id"]),
                cli=str(payload["cli"]),
                command=str(payload["command"]),
                kind=kind,
                agent_role=agent_role,
                status="queued",
                timeout_seconds=int(payload["timeout_seconds"]),
                effort=payload.get("effort"),
            )
            self.db.add(run)
            task.status = "dispatched"
            task.current_gate = "dispatch"
            if kind == "review":
                task.reviewer = str(payload["agent_id"])
            else:
                task.executor = str(payload["agent_id"])
                self._start_round(task, agent_id=str(payload["agent_id"]), run_id=run_id, now=now)
            task.dispatched_at = now
            task.error = None
            task.awaiting_approval = False
            task.approval_prompt = None
            emit_task_event(
                task_id=task.id,
                event_type="dispatched",
                payload={
                    "run_id": run_id,
                    "agent": str(payload["agent_id"]),
                    "cli": str(payload["cli"]),
                },
                db=self.db,
            )
            return run, run_id
        if gate_type == "review_order":
            reviewer = str(payload["reviewer"])
            if not task.result_ref or not task.result_ref.strip():
                raise PrerequisiteError("result_ref is required before review")
            self._require_independent(task.executor, reviewer)
            run_id = str(uuid.uuid4())
            run = AgentRun(
                id=run_id,
                task_id=task.id,
                agent_id=reviewer,
                cli=str(payload["cli"]),
                command=str(payload["command"]),
                kind="review",
                agent_role="reviewer",
                status="queued",
                timeout_seconds=int(payload["timeout_seconds"]),
            )
            self.db.add(run)
            task.reviewer = reviewer
            task.status = "in-review"
            task.current_gate = "verdict"
            task.awaiting_approval = False
            task.approval_prompt = None
            return run, run_id
        if gate_type == "verdict":
            verdict = str(payload["verdict"])
            self._validate_verdict_prerequisites(
                task,
                actor=str(payload["reviewer"]),
                verdict=verdict,
                ac_results=payload["ac_results"],
            )
            task.verdict = verdict
            task.findings = payload.get("findings") or []
            task.current_gate = "verdict"
            task.awaiting_approval = False
            task.approval_prompt = None
            if verdict == "pass":
                task.status = "done"
                task.completed_at = now
                task.final_result_ref = task.result_ref
                task.final_verdict = verdict
            else:
                task.status = "changes-requested"
                task.completed_at = None
            self._record_verdict_on_round(task, verdict=verdict, now=now)
            return None, verdict
        raise OrchestrationError(f"Unsupported gate type: {gate_type}")

    @property
    def autonomy_enabled(self) -> bool:
        return self._setting("autonomy_enabled", settings.AUTONOMY_ENABLED, bool)

    @property
    def max_cost_usd_per_task(self) -> Decimal:
        value = self._setting(
            "max_cost_usd_per_task", settings.MAX_COST_USD_PER_TASK, Decimal
        )
        return max(Decimal("0"), value)

    @property
    def max_concurrent_runs(self) -> int:
        return max(1, self._setting("max_concurrent_runs", settings.MAX_CONCURRENT_RUNS, int))

    @property
    def run_timeout_seconds(self) -> int:
        return max(1, self._setting("run_timeout_seconds", settings.RUN_TIMEOUT_SECONDS, int))

    def check_brakes(
        self,
        task: Task,
        *,
        for_spawn: bool = False,
        audit: bool = False,
        run_id: str | None = None,
    ) -> BrakeDecision:
        """Return the current safety decision using runtime DB settings.

        ``for_spawn`` checks the global active-run limit.  It is deliberately
        separate from the kill switch and budget checks: concurrency queues a
        run, while the other two brakes stop and escalate the task.
        """
        if not self.autonomy_enabled:
            decision = BrakeDecision(False, "Autonomy is disabled", "autonomy_disabled")
        else:
            cost = self._task_cost(task)
            if cost >= self.max_cost_usd_per_task:
                reason = (
                    f"Task cost limit reached: ${cost:.8f} >= "
                    f"${self.max_cost_usd_per_task:.8f}"
                )
                decision = BrakeDecision(False, reason, "cost_limit", cost_usd=cost)
            elif for_spawn:
                # PostgreSQL rejects `SELECT ... FOR UPDATE` on an aggregate
                # (func.count), so lock the individual candidate rows instead
                # and count them in Python. Ordering by id gives every caller
                # the same lock-acquisition order, which is what prevents
                # concurrent dispatches from deadlocking on this row set.
                active_query = self.db.query(AgentRun.id).filter(
                    AgentRun.status.in_(["queued", "running"])
                )
                if run_id:
                    active_query = active_query.filter(AgentRun.id != run_id)
                active = len(
                    active_query.order_by(AgentRun.id).with_for_update().all()
                )
                if active >= self.max_concurrent_runs:
                    decision = BrakeDecision(
                        False,
                        f"Concurrent run limit reached: {active} >= {self.max_concurrent_runs}",
                        "concurrency_limit",
                        queue=True,
                        cost_usd=cost,
                    )
                else:
                    decision = BrakeDecision(True, cost_usd=cost)
            else:
                decision = BrakeDecision(True, cost_usd=cost)

        if audit and not decision.allowed:
            self._record_brake(task, decision)
        return decision

    def _record_brake(self, task: Task, decision: BrakeDecision) -> None:
        if decision.code in {"autonomy_disabled", "cost_limit"}:
            task.status = "failed"
            task.error = decision.reason
            task.awaiting_approval = True
            task.approval_prompt = f"Escalated by safety brake: {decision.reason}"
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
                },
            )
        )
        self.db.commit()
        if decision.code in {"autonomy_disabled", "cost_limit"}:
            self.wake_dependents(task.id)

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

    def _validate_verdict_prerequisites(
        self,
        task: Task,
        *,
        actor: str,
        verdict: str,
        ac_results: Any,
    ) -> None:
        self._assert_status(task, "in-review")
        if not task.executor or not task.executor.strip():
            raise PrerequisiteError("executor is required for verdict")
        if not task.reviewer or not task.reviewer.strip():
            raise PrerequisiteError("reviewer is required for verdict")
        self._require_independent(task.executor, task.reviewer)
        # The reviewer identity that authorizes a verdict is never taken from
        # the caller-supplied `actor` (a coordinator/LLM tool call could claim
        # to *be* task.reviewer). It must instead come from a terminal
        # AgentRun(kind="review") this service itself created and completed —
        # that is the only proof an independent review actually ran.
        review_run = self._terminal_review_run(task.id)
        if review_run is None:
            raise PrerequisiteError(
                "verdict requires a completed review run for this task"
            )
        if self._principal(review_run.agent_id) != self._principal(task.reviewer):
            raise PrerequisiteError(
                "The completed review run's agent does not match the task's "
                "assigned reviewer"
            )
        if not task.result_ref or not task.result_ref.strip():
            raise PrerequisiteError("result_ref is required for verdict")
        evaluations = self._evaluation_results(ac_results)
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
    def _evaluation_results(ac_results: Any) -> list[bool]:
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

    def _task(self, task_id: str) -> Task:
        task = (
            self.db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update()
            .first()
        )
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        return task

    def _idempotent_record(
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

    def _reject_if_stale_dispatch_record(self, record: GateRecord) -> None:
        """Guard against handing back a dispatch "success" for a dead run.

        Only called from `_request_gate`'s idempotent-record lookup — the
        one place where a caller (via the command router's attempt-numbered
        key) is expected to mint a fresh idempotency key per genuinely new
        dispatch attempt. In normal operation that attempt bump already keeps
        a retry from ever colliding with a dead run's key, which makes this a
        defense-in-depth check for callers that construct keys directly
        (e.g. tests, or a future caller that reuses a key deliberately) —
        not the primary defense. Do NOT call this from `decide_gate`: that
        key has no attempt component and its cached record is an immutable
        decision, not a retryable request (see the comment there).

        A cached dispatch-gate record whose AgentRun has already left
        queued/running (succeeded, timed out, cancelled, or failed with all
        attempts exhausted) is no longer "in flight". Returning it as
        ``applied=True`` would make a caller believe a run is actively
        executing when none is; the caller must obtain a fresh idempotency
        key (e.g. a new attempt number) and retry instead. A "failed" run
        that hasn't exhausted its attempts is excluded: the worker retries it
        in place (status goes back to "queued"), so it is still in flight.
        """
        effective = record
        if effective.status == "pending":
            # Mirror `_result_for_record`'s pending -> decision resolution:
            # in supervised mode the record cached under the idempotency key
            # is the pending parent, while the approve/reject decision (and
            # the dispatched run) lives on a child record. Checking the
            # parent's status directly always sees "pending" and never fires,
            # which is exactly how this guard went dead in supervised mode.
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

    def _result_for_record(
        self,
        task: Task,
        record: GateRecord,
    ) -> TransitionResult:
        effective = record
        payload_source = record
        if record.status == "pending":
            decision = (
                self.db.query(GateRecord)
                .filter(GateRecord.parent_id == record.id)
                .order_by(GateRecord.id.desc())
                .first()
            )
            if decision is not None:
                effective = decision
        elif record.parent_id is not None:
            parent = self.db.get(GateRecord, record.parent_id)
            if parent is not None:
                payload_source = parent

        run = (
            self.db.get(AgentRun, effective.output_ref)
            if effective.gate_type == "dispatch" and effective.output_ref
            else None
        )
        return TransitionResult(
            task=task,
            gate_record=effective,
            applied=effective.status == "approved",
            agent_run=run,
            context=payload_source.input_payload if run is not None else None,
        )

    def _ledger_record(
        self,
        *,
        task: Task,
        gate_type: str,
        status: str,
        actor: str,
        idempotency_key: str,
        input_hash: str,
        payload: dict[str, Any],
        output_ref: str | None = None,
        output_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        parent_id: int | None = None,
    ) -> GateRecord:
        record = GateRecord(
            task_id=task.id,
            gate_type=gate_type,
            status=status,
            actor=actor,
            mode=task.mode,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            output_ref=output_ref,
            parent_id=parent_id,
            executor=task.executor,
            reviewer=task.reviewer,
            input_payload=payload,
            output_payload=output_payload,
            error_message=error_message,
        )
        self.db.add(record)
        return record

    def _audit(
        self,
        task: Task,
        record: GateRecord,
        *,
        reason: str | None = None,
    ) -> None:
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
                    "reason": reason,
                },
            )
        )

    @staticmethod
    def _gate_output(task: Task, gate_type: str) -> dict[str, Any]:
        return {
            "gate_type": gate_type,
            "task_status": task.status,
            "current_gate": task.current_gate,
            "executor": task.executor,
            "reviewer": task.reviewer,
            "result_ref": task.result_ref,
            "verdict": task.verdict,
        }

    @classmethod
    def _validate_common(
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
    def _assert_status(task: Task, expected_status: str) -> None:
        if task.status != expected_status:
            raise TransitionConflictError(
                f"Task {task.id} expected status {expected_status!r}, "
                f"found {task.status!r}"
            )

    def _terminal_review_run(self, task_id: str) -> AgentRun | None:
        return (
            self.db.query(AgentRun)
            .filter(
                AgentRun.task_id == task_id,
                AgentRun.kind == "review",
                AgentRun.status == "success",
            )
            .order_by(AgentRun.queued_at.desc())
            .first()
        )

    @classmethod
    def _require_independent(
        cls,
        executor: str | None,
        reviewer: str | None,
    ) -> None:
        if not executor or not executor.strip():
            raise PrerequisiteError("executor is required")
        if not reviewer or not reviewer.strip():
            raise PrerequisiteError("reviewer is required")
        if cls._principal(executor) == cls._principal(reviewer):
            raise PrerequisiteError("Reviewer must differ from executor")

    @staticmethod
    def _principal(value: str) -> str:
        return value.strip().casefold()

    @staticmethod
    def _input_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
