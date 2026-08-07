from __future__ import annotations

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import exists, func, update
from sqlalchemy.orm import Session, aliased

from app.db.models import (
    Agent,
    AgentRun,
    AuditLog,
    DispatchCandidate,
    DispatchDecision,
    GateRecord,
    ImplDesign,
    LLMUsage,
    Project,
    ReviewCycle,
    ReviewFinding,
    SpecTaskLink,
    Task,
    TaskDependency,
    TaskRound,
)
from app.db.models import Session as SessionModel
from app.services.agent_matcher import POLICY_VERSION as AGENT_MATCHER_POLICY_VERSION
from app.services.agent_matcher import AgentMatcher
from app.services.agent_run_classification import classify_termination
from app.services.approval_hold import derive_approval_hold
from app.services.entity_admin import EntityValidationError, _validate_cli_model
from app.services.landing import LandingResult, head_of, land_result
from app.services.outbox import record_commit_event, record_run_requested
from app.services.review_criteria import merged_review_criteria
from app.services.spec_anchor import link_task_to_changed_specs
from app.services.task_event_service import emit_task_event
from app.services.task_validators import (
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

GateDecision = Literal["approved", "rejected"]

from app.services.fsm.verdict_landing import TransitionResult, _split_result_range, _review_finding_from_payload


def find_active_plan_run(db: Session, task_id: str) -> AgentRun | None:
    """The queued/running planner AgentRun for a task, if any (CTV2-1396).

    task.open_questions / task.spec_clarity are overwritten only when a
    plan run finishes (write_spec_plan). While a new run is in flight the
    columns still hold the *previous* round's values -- callers that surface
    open_questions to the coordinator must be able to say so, instead of
    letting the coordinator conclude the planner ignored an update and
    answer the same questions again.
    """
    return (
        db.query(AgentRun)
        .filter(
            AgentRun.task_id == task_id,
            AgentRun.status.in_(["queued", "running"]),
            AgentRun.idempotency_key.like("planner:%"),
        )
        .order_by(AgentRun.queued_at.desc())
        .first()
    )

def _plan_critic_token_budget() -> int:
    """The critic's real token ceiling, read at call time.

    Local import: spec_plan_generator does not import this module today, but
    keeping the dependency lazy means raising the budget never has to worry
    about import order.
    """
    from app.services.spec_plan_generator import PLAN_CRITIC_TOKEN_BUDGET

    return PLAN_CRITIC_TOKEN_BUDGET

def _is_cheap_executor(agent: Agent) -> bool:
    """Identify the explicitly low-cost executor tier for impl-design gating."""

    effort = (agent.effort or "").strip().lower()
    model = f"{agent.model or ''} {agent.name or ''}".lower()
    return effort == "low" or "flash" in model or "mini" in model

class TaskLifecycleMixin:
        def sync_awaiting_approval(self, task: Task, *, as_status: str | None = None) -> bool:
            """Write the approval projection back from the one function that derives it.

            This is the ONLY place allowed to assign `task.awaiting_approval` or
            `task.approval_prompt`.  Both come out of the same `ApprovalHold`, so
            the two columns cannot disagree with each other, and neither can
            disagree with the evidence -- gate ledger, human_question events, spec
            clarity, plan critic verdict, landing error (CTV2-1401, spec item
            3e2a7102).

            Callers that *refuse to act* on a hold must not read the column this
            writes; they call `derive_approval_hold` themselves.  A cached value
            can be one beat stale, and a stale value must never be able to brick a
            task again.
            """
            self.db.flush()
            hold = derive_approval_hold(self.db, task, as_status=as_status)
            task.awaiting_approval = hold is not None
            task.approval_prompt = hold.prompt if hold is not None else None
            return hold is not None

        def _sync_after_transition(self, task: Task) -> None:
            if task.status in {"done", "failed", "cancelled"}:
                self._cancel_active_runs(task)
                self._reject_all_pending_gates(
                    task, f"Task reached terminal state: {task.status}"
                )
            else:
                self.sync_awaiting_approval(task)

        def _cancel_active_runs(self, task: Task) -> int:
            """Cancel runs that must not outlive a terminal task projection.

            This is an atomic status claim, not a direct process-side effect.  A
            worker observing the cancelled row exits through its normal cancel
            path, so the DB transition and the process stop remain retry-safe.
            """
            now = datetime.now(timezone.utc)
            self.db.flush()
            result = self.db.execute(
                update(AgentRun)
                .where(
                    AgentRun.task_id == task.id,
                    AgentRun.status.in_(["queued", "running"]),
                )
                .values(
                    status="cancelled",
                    error_message=f"Task reached terminal state: {task.status}",
                    completed_at=now,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            return int(result.rowcount or 0)

        def audit(
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

        def start_round(
            self,
            task: Task,
            *,
            agent_id: str,
            run_id: str,
            now: datetime,
        ) -> TaskRound:
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
            self.db.flush()
            task.current_round_id = round_id
            return task_round

        def prior_executor_result_ref(self, task: Task) -> str | None:
            """Return the immediately prior executor range for a re-dispatch.

            A changes-requested task has already completed its previous execute
            round.  Prefer the round snapshot when available, with the task
            projection as a compatibility fallback for older/imported rows.
            """
            prior_round = (
                self.db.query(TaskRound)
                .filter(
                    TaskRound.task_id == task.id,
                    TaskRound.result_ref.isnot(None),
                )
                .order_by(TaskRound.round_no.desc())
                .first()
            )
            return (prior_round.result_ref if prior_round else None) or task.result_ref

        def prior_executor_head(self, task: Task) -> str | None:
            """Return the prior executor head, if the task has a committed range."""
            return head_of(self.prior_executor_result_ref(task))

        def record_dispatch_decision(
            self,
            *,
            task: Task,
            kind: str,
            idempotency_key: str,
            selected_agent_id: str,
        ) -> str:
            decision_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"dispatch-decision:{task.id}:{idempotency_key}:{kind}",
                )
            )
            if self.db.get(DispatchDecision, decision_id) is not None:
                return decision_id

            exclude_agent_id = task.executor if kind == "review" else None
            scoring = AgentMatcher(self.db).score_candidates(
                task, top_n=1, exclude_agent_id=exclude_agent_id
            )
            selected = next(
                (c for c in scoring.candidates if c.agent_id == selected_agent_id), None
            )
            top_choice = scoring.suggestions[0].agent_id if scoring.suggestions else None

            self.db.add(
                DispatchDecision(
                    id=decision_id,
                    task_id=task.id,
                    task_round_id=task.current_round_id,
                    kind=kind,
                    policy_version=AGENT_MATCHER_POLICY_VERSION,
                    task_feature_snapshot=scoring.feature_snapshot,
                    selected_agent_id=selected_agent_id,
                    selected_score=selected.final_score if selected else None,
                    selection_reason=(
                        selected.reason
                        if selected and selected.reason
                        else "selected outside matcher ranking"
                    ),
                    exploration=False,
                    human_override=bool(top_choice) and top_choice != selected_agent_id,
                )
            )
            self.db.add_all(
                DispatchCandidate(
                    dispatch_decision_id=decision_id,
                    agent_id=candidate.agent_id,
                    eligible=candidate.eligible,
                    rejection_reason=candidate.rejection_reason,
                    predicted_pass1=candidate.predicted_pass1,
                    predicted_runtime=candidate.predicted_runtime,
                    quota_pressure=candidate.quota_pressure,
                    final_score=candidate.final_score,
                )
                for candidate in scoring.candidates
            )
            return decision_id

        def _abandon_review_cycle(self, run_id: str | None) -> None:
            """A review AgentRun died (failed/timeout/brake) without ever
            reaching a verdict. Mark its cycle 'abandoned' so it reads as
            "dead with no outcome" instead of stuck at 'running'/'requested'
            forever (CTV2-1379). A cycle that already reached submitted/pass/
            changes/abandoned is left alone -- this only catches runs that died
            before producing anything.
            """
            if not run_id:
                return
            cycle = (
                self.db.query(ReviewCycle)
                .filter(ReviewCycle.reviewer_agent_run_id == run_id)
                .first()
            )
            if cycle is not None and cycle.status in {"requested", "running"}:
                cycle.status = "abandoned"
                cycle.completed_at = datetime.now(timezone.utc)

        def transition(self, action: str, **kwargs: Any) -> TransitionResult:
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
            task = self.validator.task(task_id)
            if kind == "execute" and (task.open_questions or []):
                question_count = len(task.open_questions)
                active_run = find_active_plan_run(self.db, task.id)
                if active_run is not None:
                    started = active_run.started_at or active_run.queued_at
                    raise PrerequisiteError(
                        f"Spec has {question_count} open questions, but these are from a "
                        f"PREVIOUS round -- plan run {active_run.id} is already running "
                        f"(started {started}) and will overwrite them. Do not answer again; "
                        "wait for that run to finish, then re-check open_questions."
                    )
                raise PrerequisiteError(
                    f"Spec has {question_count} unanswered open questions; answer them and "
                    "re-run generate_spec_plan before dispatch."
                )
            if expected_status is None:
                if kind == "review":
                    expected_status = "awaiting-review"
                elif task.status in {"failed", "changes-requested"}:
                    expected_status = task.status
                else:
                    expected_status = "todo"
            elif kind == "execute" and expected_status not in {
                "todo", "failed", "changes-requested",
            }:
                raise PrerequisiteError(
                    "execute dispatch requires expected_status in "
                    "{'todo', 'failed', 'changes-requested'}"
                )
            if not merged_review_criteria(task.acceptance_criteria, task.constraints) and not task.legacy_no_ac:
                raise PrerequisiteError(
                    "dispatch requires acceptance_criteria or constraints; run the spec/plan gate "
                    "first (or set legacy_no_ac for pre-existing tasks)"
                )
            if kind == "execute" and task.planner and task.plan_critic_status != "accept":
                raise PrerequisiteError(
                    "dispatch is blocked until the current generated plan is accepted by "
                    "an independent plan critic"
                )
            agent = self.db.get(Agent, agent_id)
            if agent is None:
                raise PrerequisiteError(f"Agent {agent_id} not found")
            agent_type = getattr(agent.agent_type, "value", agent.agent_type)
            if agent_type == "cli" and agent.model:
                try:
                    _validate_cli_model(self.db, agent.cli, agent.model)
                except EntityValidationError as exc:
                    raise PrerequisiteError(str(exc)) from exc
            if kind == "execute" and _is_cheap_executor(agent):
                design = (
                    self.db.query(ImplDesign)
                    .filter(ImplDesign.task_id == task.id)
                    .first()
                )
                if design is not None and not bool((design.completeness or {}).get("passed")):
                    raise PrerequisiteError(
                        f"impl_design for task {task.id} has not passed all six completeness checks; "
                        "score_completeness or revise the design before dispatching a cheap executor"
                    )
            if kind == "review":
                self.validator.require_independent(task.executor, agent_id)
            project = self.db.get(Project, task.project)
            resolved_effort = effort or agent.effort or "medium"
            resolved_timeout = timeout_seconds or self.validator.run_timeout_seconds
            try:
                import app.services.task_orchestration as task_orch_module

                command, repo_root, cli = task_orch_module.build_dispatch_command(
                    task,
                    agent,
                    project,
                    effort=resolved_effort,
                    db=self.db,
                    timeout_seconds=resolved_timeout,
                )
            except ValueError as exc:
                raise PrerequisiteError(str(exc)) from exc

            dispatch_decision_id = self.record_dispatch_decision(
                task=task,
                kind=kind,
                idempotency_key=idempotency_key,
                selected_agent_id=agent_id,
            )

            return self.request_gate(
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
                    "timeout_seconds": resolved_timeout,
                    "kind": kind,
                    "agent_role": "reviewer" if kind == "review" else "executor",
                    "effort": resolved_effort,
                    "brake_agent_id": agent_id,
                    "dispatch_decision_id": dispatch_decision_id,
                },
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
            task = self.validator.task(task_id)
            if not task.result_ref or not task.result_ref.strip():
                raise PrerequisiteError("result_ref is required before review")
            if not reviewer or not reviewer.strip():
                raise PrerequisiteError("reviewer is required")
            reviewer = reviewer.strip()
            if not task.executor or not task.executor.strip():
                raise PrerequisiteError("executor is required")
            agent = self.db.get(Agent, reviewer)
            invalid_reason: str | None = None
            if self.validator.principal(task.executor) == self.validator.principal(reviewer):
                invalid_reason = (
                    "violates four-eyes; reviewer must differ from executor "
                    f"{task.executor!r}"
                )
            elif agent is None:
                invalid_reason = "does not exist"
            elif (agent.status or "").strip().lower() in self.validator._UNAVAILABLE_REVIEWER_STATUSES:
                invalid_reason = f"has status {agent.status!r}"
            if invalid_reason is not None:
                suggestions = AgentMatcher(self.db).suggest_agents(
                    task, top_n=3, exclude_agent_id=task.executor
                )
                suggestion_text = ", ".join(item.agent_id for item in suggestions)
                if not suggestion_text:
                    suggestion_text = "none currently available"
                raise PrerequisiteError(
                    f"Requested reviewer {reviewer!r} is invalid: {invalid_reason}. "
                    f"Valid reviewer suggestions: {suggestion_text}"
                )
            self.validator.require_independent(task.executor, reviewer)
            base_ref, head_ref = _split_result_range(task.result_ref)
            if not base_ref or not head_ref:
                raise PrerequisiteError(
                    "result_ref must be a recorded base..head range before review "
                    "(the review boundary is never inferred)"
                )
            project = self.db.get(Project, task.project)
            resolved_timeout = timeout_seconds or self.validator.run_timeout_seconds
            try:
                import app.services.task_orchestration as task_orch_module

                command, repo_root, cli = task_orch_module.build_review_command(
                    task,
                    agent,
                    project,
                    base_ref,
                    head_ref,
                    db=self.db,
                    timeout_seconds=resolved_timeout,
                )
            except ValueError as exc:
                raise PrerequisiteError(str(exc)) from exc

            return self.request_gate(
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
                    "timeout_seconds": resolved_timeout,
                    "selection_reason": (
                        selection_reason.strip()
                        if selection_reason and selection_reason.strip()
                        else f"{reviewer} was explicitly requested by {actor}"
                    ),
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
            review_cycle_id: str,
            findings: list[Any] | None = None,
            expected_status: str = "in-review",
        ) -> TransitionResult:
            task = self.validator.task(task_id)
            normalized_verdict = verdict.strip().lower()
            if normalized_verdict not in {"pass", "changes"}:
                raise PrerequisiteError("Verdict must be pass or changes")
            # The reviewer submitting a verdict is what moves a cycle from
            # 'running' to 'submitted' -- do it before validating so a fresh,
            # legitimate submission is not rejected for "not submitted yet".
            cycle = self.db.get(ReviewCycle, review_cycle_id) if review_cycle_id else None
            if (
                cycle is not None
                and cycle.task_id == task.id
                and cycle.status in {"requested", "running"}
            ):
                cycle.status = "submitted"
                cycle.submitted_at = datetime.now(timezone.utc)
                self.db.flush()
            self.validator.validate_verdict_prerequisites(
                task,
                actor=actor,
                verdict=normalized_verdict,
                ac_results=ac_results,
                review_cycle_id=review_cycle_id,
            )
            return self.request_gate(
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
                    "review_cycle_id": review_cycle_id,
                },
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
            task = self.validator.task(task_id)
            payload = {
                "expected_status": expected_status,
                "result_ref": result_ref,
                "run_id": run_id,
            }
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            self.validator.assert_status(task, expected_status)
            now = datetime.now(timezone.utc)
            self.cas_status(task, "awaiting-review")
            task.current_gate = "review_order"
            task.result_ref = result_ref
            task.error = None
            task.updated_at = now
            record = self.ledger_record(
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
            self._sync_after_transition(task)
            self.audit(task, record)
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
            error_code: str | None = None,
        ) -> TransitionResult:
            run = self.db.get(AgentRun, run_id) if run_id else None
            if run is not None:
                run.failure_category = classify_termination(
                    status="failed", error=error, kind=run.kind
                )
                if run.kind == "review":
                    self._abandon_review_cycle(run_id)
            task = self.validator.task(task_id)
            normalized_error_code = (error_code or "execution-failed").strip().lower()
            payload = {
                "expected_status": expected_status,
                "error_code": normalized_error_code,
                "run_id": run_id,
            }
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            self.validator.assert_status(task, expected_status)
            self.cas_status(task, "failed")
            task.error = error
            task.updated_at = datetime.now(timezone.utc)
            record = self.ledger_record(
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
            self._sync_after_transition(task)
            self.audit(task, record, reason=error)
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
            error_details: dict[str, Any] | None = None,
        ) -> TransitionResult:
            run = self.db.get(AgentRun, run_id) if run_id else None
            if run is not None:
                run.failure_category = classify_termination(
                    status="failed", error=error, kind=run.kind
                )
                self._abandon_review_cycle(run_id)
            task = self.validator.task(task_id)
            payload = {
                "expected_status": expected_status,
                "error": error,
                "run_id": run_id,
            }
            # Keep the legacy idempotency hash stable for non-parser failures.
            if error_details is not None:
                payload["error_details"] = error_details
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            self.validator.assert_status(task, expected_status)
            now = datetime.now(timezone.utc)
            # A malformed reviewer artifact says nothing about the executor's
            # committed result.  Return to the review boundary so another
            # independent reviewer can be assigned without attach_result.
            self.cas_status(task, "awaiting-review")
            task.error = error
            task.current_gate = "review_order"
            task.updated_at = now
            record = self.ledger_record(
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
            self._sync_after_transition(task)
            self.audit(task, record, reason=error)
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            return TransitionResult(task, record, True)

        def reopen_failed_task(self, *, task_id: str, actor: str) -> TransitionResult:
            """Bring a ``failed`` task back to a state work can continue from.

            ``failed`` is terminal: ``_sync_after_transition`` cancels every active
            run and rejects every pending gate, and every entry point refuses it --
            ``request_review`` wants ``awaiting-review``, ``attach_result`` wants
            ``dispatched``, ``land_task`` wants an approved verdict, and a freshly
            requested planner/critic run is cancelled on arrival with "Task reached
            terminal state: failed".  There was no way back in the whole tool
            surface.

            That mattered because tasks reach ``failed`` for reasons that say
            nothing about the work: a budget brake firing one second after a
            successful result was delivered (CTV2-1382), or the orchestration
            driver escalating because a plan critic had not finished yet
            (CTV2-1388 -- the task filed to fix CTV2-1382, killed by the same
            defect).  Both were recoverable situations recorded as terminal ones.

            Where the task lands is decided by evidence, not by the caller:

            * a delivered ``result_ref`` -> ``awaiting-review``, so an independent
              reviewer still has to pass it before it can land
            * otherwise -> ``todo``, so it goes back through dispatch

            Four-eyes is untouched: reopening restores the *boundary*, never a
            verdict.  The ledger stays append-only -- this appends a ``reopen``
            record rather than editing the escalation that closed the task.
            """

            task = self.validator.task(task_id)
            if task.status != "failed":
                # Second way to be stuck: the task is not terminal, but its stored
                # approval projection says a human is being waited on while no
                # source of waiting actually exists.  CTV2-1389 landed there when
                # escalations changed from `rejected` to `pending` records: its
                # flag was set by the old shape, so it sat on a finished,
                # critic-accepted plan it could not act on.
                #
                # Since CTV2-1401 the blocking decisions derive the answer instead
                # of reading this column, so a drifted value no longer bricks a
                # task -- but rows written before that fix still carry one, and
                # this remains the way to write the truth back over them.
                if task.awaiting_approval and not self.sync_awaiting_approval(task):
                    task.error = None
                    task.updated_at = datetime.now(timezone.utc)
                    payload = {
                        "from_status": task.status,
                        "to_status": task.status,
                        "reason": "cleared a stale approval projection",
                    }
                    record = self.ledger_record(
                        task=task,
                        gate_type="reopen",
                        status="approved",
                        actor=actor,
                        idempotency_key=str(uuid.uuid4()),
                        input_hash=TaskValidator.input_hash(payload),
                        payload=payload,
                        output_payload={"status": task.status},
                    )
                    self.audit(task, record, reason="cleared a stale approval projection")
                    self.db.commit()
                    self.db.refresh(task)
                    self.db.refresh(record)
                    return TransitionResult(task, record, True)
                raise PrerequisiteError(
                    f"reopen requires status 'failed', found {task.status!r}"
                )
            delivered = bool((task.result_ref or "").strip())
            target = "awaiting-review" if delivered else "todo"
            previous_error = task.error
            failure_record = (
                self.db.query(GateRecord)
                .filter(
                    GateRecord.task_id == task.id,
                    GateRecord.parent_id.is_(None),
                    GateRecord.status == "rejected",
                    GateRecord.gate_type.in_(
                        ["safety_brake", "execution", "escalation", "dispatch_queue"]
                    ),
                )
                .order_by(GateRecord.id.desc())
                .first()
            )
            payload = {
                "from_status": "failed",
                "to_status": target,
                "result_ref": task.result_ref,
                "previous_error": previous_error,
            }
            self.cas_status(task, target)
            # Clearing the error clears the evidence for the `landing` hold too;
            # `_sync_after_transition` below rewrites the projection from it.
            task.error = None
            task.current_gate = "review_order" if delivered else "dispatch"
            task.updated_at = datetime.now(timezone.utc)
            record = self.ledger_record(
                task=task,
                gate_type="reopen",
                status="approved",
                actor=actor,
                idempotency_key=str(uuid.uuid4()),
                input_hash=TaskValidator.input_hash(payload),
                payload=payload,
                output_payload={"status": target},
                parent_id=failure_record.id if failure_record is not None else None,
            )
            self._sync_after_transition(task)
            self.audit(task, record, reason=f"reopened from failed -> {target}")
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            return TransitionResult(task, record, True)

        def escalate_task(
            self, *, task_id: str, reason: str, actor: str = "system"
        ) -> GateRecord:
            task = self.validator.task(task_id)
            # An escalation means "stop and ask a human", not "this task is dead".
            # Every reason that reaches here is human-actionable: missing
            # acceptance criteria (add them), a failed dependency (reopen it), no
            # available executor or reviewer (enable an agent), a transient
            # dispatch/review exception (retry), the changes-requested round limit
            # (whose own message says "escalating for a human replan"), or
            # advance_task making no progress.
            #
            # It used to also set `failed`, which is terminal:
            # `_sync_after_transition` then cancelled every active run and rejected
            # every pending gate, so the very step that was about to unblock the
            # task got killed and nothing could resolve the prompt this function
            # had just written.  The system rang the bell for a human and locked
            # the door.  On 2026-08-05 that killed CTV2-1382's delivered result and
            # then CTV2-1388, the task filed to fix it.
            #
            # `awaiting_approval` is what actually stops the loop:
            # `check_brakes` returns `pending_gate` for it (task_validators.py), so
            # no new run is spawned while a human is being waited on.  The task
            # keeps its status and stays recoverable.
            task.error = reason
            task.updated_at = datetime.now(timezone.utc)
            # Written as a PENDING gate, not a rejected one.
            #
            # The escalation has to actually block work, and `awaiting_approval` is
            # the only thing left doing that now the status stays non-terminal.
            # But that flag is not free-standing: `sync_awaiting_approval`
            # recomputes it from unresolved *pending* gate records, so an
            # escalation written as `rejected` gets its own block erased on the
            # next sync -- and `approve_gate` refuses it too ("No pending gate
            # found"), leaving nothing that could ever clear it.  CTV2-1389 hit
            # exactly that: escalated, non-terminal, and undispatchable.
            #
            # As a pending gate it is honest in every direction: it appears in
            # `pending_approvals`, `approve_gate` resolves it by appending a
            # decision child (the ledger stays append-only), and the projection
            # follows from the ledger instead of being asserted on the side.
            payload = {
                "reason": reason,
                "task_status": task.status,
                "expected_status": task.status,
            }
            record = self.ledger_record(
                task=task,
                gate_type="escalation",
                status="pending",
                actor=actor,
                idempotency_key=str(uuid.uuid4()),
                input_hash=TaskValidator.input_hash(payload),
                payload=payload,
                error_message=reason,
            )
            # The projection now follows from the ledger: the pending escalation
            # record above makes `sync_awaiting_approval` compute `True` on its
            # own, so there is nothing to assert on the side.
            self._sync_after_transition(task)
            self.audit(task, record, reason=reason)
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            return record

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
            task = self.validator.task(run.task_id)
            expected_status = "in-review" if run.kind == "review" else "dispatched"
            reset_status = "awaiting-review" if run.kind == "review" else "todo"
            payload = {"run_id": run_id, "error": error}
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task.id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            self.validator.assert_status(task, expected_status)
            now = datetime.now(timezone.utc)
            run.status = "failed"
            run.error_message = error
            run.failure_category = classify_termination(
                status="failed", error=error, kind=run.kind
            )
            run.completed_at = now
            self.cas_status(task, reset_status)
            task.error = error
            task.updated_at = now
            record = self.ledger_record(
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
            self._sync_after_transition(task)
            self.audit(task, record, reason=error)
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
            reason: str = "Cancelled by user",
        ) -> TransitionResult:
            run = self.db.get(AgentRun, run_id)
            if run is None:
                raise TransitionConflictError(f"Run {run_id} not found")
            task = self.validator.task(run.task_id)
            if run.kind == "review":
                was_active = run.status in {"queued", "running"}
                if not was_active and run.status != "cancelled":
                    raise TransitionConflictError(
                        f"Cannot cancel run in status: {run.status}"
                    )
                if was_active:
                    run.status = "cancelled"
                    run.error_message = reason
                    run.failure_category = "cancelled"
                    run.completed_at = datetime.now(timezone.utc)
                    self.db.flush()
                failure = self.record_review_failure(
                    task_id=task.id,
                    error=reason,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    run_id=run.id,
                )
                if was_active:
                    emit_task_event(
                        task_id=task.id,
                        event_type="cancelled",
                        payload={"run_id": run.id, "cancelled_by": actor},
                        db=self.db,
                    )
                self.db.refresh(run)
                return TransitionResult(
                    task=failure.task,
                    gate_record=failure.gate_record,
                    applied=failure.applied,
                    agent_run=run,
                )

            payload = {"run_id": run_id, "reason": reason}
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task.id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            if run.status not in {"queued", "running"}:
                raise TransitionConflictError(
                    f"Cannot cancel run in status: {run.status}"
                )
            now = datetime.now(timezone.utc)
            run.status = "cancelled"
            run.error_message = reason
            run.failure_category = "cancelled"
            run.completed_at = now
            if task.status == "dispatched":
                self.cas_status(task, "todo")
            task.error = reason
            task.updated_at = now
            record = self.ledger_record(
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
            self._sync_after_transition(task)
            self.audit(task, record)
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
            task = self.validator.task(task_id)
            if not actor or not actor.strip():
                raise PrerequisiteError("actor is required")
            if not patch:
                raise PrerequisiteError("patch must include at least one field")
            patchable_fields = {"plan", "acceptance_criteria", "priority", "tags", "raw_input"}
            unknown = set(patch) - patchable_fields
            if unknown:
                raise PrerequisiteError(
                    f"Cannot patch fields: {', '.join(sorted(unknown))}. "
                    f"Allowed fields: {', '.join(sorted(patchable_fields))}"
                )

            self.cas_status(task, task.status)
            if {"plan", "acceptance_criteria", "raw_input"} & set(patch):
                # A critic verdict applies only to the exact generated contract.
                task.plan_critic_status = None
                task.plan_critic_findings = []
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
            task = self.validator.task(task_id)
            if not actor or not actor.strip():
                raise PrerequisiteError("actor is required")
            self.validator.assert_status(task, "todo")
            if not acceptance_criteria and not constraints:
                raise PrerequisiteError(
                    "acceptance_criteria and constraints must not both be empty"
                )
            if spec_clarity not in {"high", "medium", "low"}:
                raise PrerequisiteError("spec_clarity must be high, medium, or low")
            if not evidence:
                raise PrerequisiteError("evidence must not be empty")
            if risk == "high" and not limits:
                raise PrerequisiteError("limits are required when risk is high")

            task.acceptance_criteria = acceptance_criteria
            task.constraints = constraints
            task.evidence = evidence
            task.prior_art = prior_art
            task.ruled_out = ruled_out
            task.limits = limits
            task.planner = planner
            task.plan_critic = None
            task.plan_critic_status = None
            task.plan_critic_findings = []
            task.plan = plan
            task.files = files
            task.tests = tests
            task.risk = risk
            task.spec_clarity = spec_clarity
            task.open_questions = open_questions
            if task.mode == "supervised":
                task.mode = self.validator.mode_for_task(task, risk=risk)
            task.flows = flows
            task.current_gate = "plan"
            if not (open_questions or spec_clarity != "high"):
                task.open_questions = []
            # `spec_clarity` + `open_questions` are themselves the evidence for the
            # Spec Clarity hold, so the prompt is derived from them rather than
            # written alongside them.  A later `update_task` that answers the
            # questions therefore cannot leave a contradictory prompt behind.
            self.sync_awaiting_approval(task)
            task.updated_at = datetime.now(timezone.utc)
            payload = {
                "acceptance_criteria": acceptance_criteria,
                "constraints": constraints,
                "evidence": evidence,
                "prior_art": prior_art,
                "ruled_out": ruled_out,
                "limits": limits,
                "plan": plan,
                "files": files,
                "tests": tests,
                "risk": risk,
                "flows": flows,
                "spec_clarity": spec_clarity,
                "open_questions": open_questions,
            }
            self.cas_status(task, task.status)
            record = self.ledger_record(
                task=task,
                gate_type="spec_plan",
                status="approved",
                actor=actor,
                idempotency_key=str(uuid.uuid4()),
                input_hash=TaskValidator.input_hash(payload),
                payload=payload,
                output_payload=self.gate_output(task, "spec_plan"),
                executor=planner,
            )
            self.audit(task, record)
            self.db.add(
                AuditLog(
                    task_id=task.id,
                    action="spec_plan_generated",
                    actor=actor,
                    details={
                        "acceptance_count": len(acceptance_criteria),
                        "constraint_count": len(constraints),
                        "review_criteria_count": len(acceptance_criteria) + len(constraints),
                        "files": files,
                        "flows": flows,
                        "risk": risk,
                        "spec_clarity": spec_clarity,
                        "open_question_count": len(open_questions),
                    },
                )
            )
            self.db.commit()
            self.db.refresh(task)

            if critic is not None or critic_verdict is not None:
                return self.record_plan_critic_verdict(
                    task_id=task_id,
                    actor=actor,
                    critic=critic,
                    verdict=critic_verdict,
                    findings=critic_findings or [],
                    summary=critic_summary or "",
                    tokens=critic_tokens or 0,
                )
            return task

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
            """Run the critic step against whatever plan is currently on the task.

            Reads ``task.planner`` from the DB rather than accepting it as a
            parameter, so the caller cannot silently attribute a verdict to the
            wrong planner. Appends exactly one ``plan_critic`` GateRecord per
            call — re-critiquing (e.g. after a rejected round) appends another,
            it never rewrites the previous one.
            """

            task = self.validator.task(task_id)
            if not actor or not actor.strip():
                raise PrerequisiteError("actor is required")
            if verdict not in {"accept", "reject"}:
                raise PrerequisiteError("critic_verdict must be accept or reject")
            if verdict == "reject" and not findings:
                raise PrerequisiteError("critic rejection requires evidenced findings")
            for finding in findings:
                if not isinstance(finding, dict) or not finding.get("evidence"):
                    raise PrerequisiteError(
                        "every critic rejection finding requires reproducible evidence"
                    )
            self.validator.require_independent(task.planner, critic)

            task.plan_critic = critic
            task.plan_critic_status = verdict
            task.plan_critic_findings = findings
            # `plan_critic_status` is the evidence.  The prompt is derived from it
            # (and from the findings), so replanning -- which resets the status --
            # cannot leave a rejection prompt standing over an accepted plan.
            self.sync_awaiting_approval(task)
            task.updated_at = datetime.now(timezone.utc)
            self.cas_status(task, task.status)
            critic_payload = {
                "planner": task.planner,
                "critic": critic,
                "verdict": verdict,
                "findings": findings,
                "summary": summary,
                # Read the real budget instead of restating it. This was hardcoded
                # 50_000 and silently went stale the moment PLAN_CRITIC_TOKEN_BUDGET
                # was raised to 150_000 — and because GateRecord is append-only, a
                # wrong number here is written permanently into the ledger and
                # cannot be corrected later. Imported locally: spec_plan_generator
                # does not import this module, but keeping it lazy avoids creating
                # that edge for future changes.
                "token_budget": _plan_critic_token_budget(),
                "tokens_used": tokens,
                "diff_provided": False,
            }
            critic_record = self.ledger_record(
                task=task,
                gate_type="plan_critic",
                status="approved" if verdict == "accept" else "rejected",
                actor=critic,
                idempotency_key=str(uuid.uuid4()),
                input_hash=TaskValidator.input_hash(critic_payload),
                payload=critic_payload,
                output_payload={
                    "verdict": verdict,
                    "findings": findings,
                    "summary": summary,
                    "tokens_used": tokens,
                },
                error_message=summary if verdict == "reject" else None,
                executor=task.planner,
                reviewer=critic,
            )
            self.audit(task, critic_record)
            self.db.add(
                AuditLog(
                    task_id=task.id,
                    action="plan_critic_recorded",
                    actor=actor,
                    details={
                        "critic": critic,
                        "critic_verdict": verdict,
                        "critic_tokens": tokens,
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
            task = self.validator.task(task_id)
            payload = {"expected_status": expected_status}
            input_hash = TaskValidator.input_hash(payload)
            existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
            if existing is not None:
                return self.result_for_record(task, existing)
            self.validator.assert_status(task, expected_status)
            self.cas_status(task, "todo")
            task.current_gate = "plan"
            task.verdict = None
            task.updated_at = datetime.now(timezone.utc)
            record = self.ledger_record(
                task=task,
                gate_type="replan",
                status="approved",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=payload,
                output_payload={"task_status": task.status},
            )
            self.audit(task, record)
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            return TransitionResult(task, record, True)

        def changes_round_count(self, task_id: str) -> int:
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
            if task_id == depends_on_task_id:
                raise DependencyCycleError(f"Task {task_id} cannot depend on itself")
            if self.db.get(Task, task_id) is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            if self.db.get(Task, depends_on_task_id) is None:
                raise TaskNotFoundError(f"Task {depends_on_task_id} not found")

            existing = self.db.get(TaskDependency, (task_id, depends_on_task_id))
            if existing is not None:
                return existing

            if self.validator.creates_cycle(task_id, depends_on_task_id):
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
