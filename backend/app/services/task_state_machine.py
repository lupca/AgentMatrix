"""State machine transitions, gate execution, and ledger management for tasks."""

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
    Project,
    Task,
    TaskDependency,
    TaskRound,
)
from app.db.models import Session as SessionModel
from app.services.agent_matcher import AgentMatcher, POLICY_VERSION as AGENT_MATCHER_POLICY_VERSION
from app.services.landing import LandingResult, head_of, land_result
from app.services.outbox import record_commit_event, record_run_requested
from app.services.review_criteria import merged_review_criteria
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


def _is_cheap_executor(agent: Agent) -> bool:
    """Identify the explicitly low-cost executor tier for impl-design gating."""

    effort = (agent.effort or "").strip().lower()
    model = f"{agent.model or ''} {agent.name or ''}".lower()
    return effort == "low" or "flash" in model or "mini" in model


def _split_result_range(result_ref: str) -> tuple[str | None, str | None]:
    """Expose the committed review range to the review gate/run context."""
    if ".." not in result_ref:
        return None, result_ref
    base, head = result_ref.split("..", 1)
    return base or None, head or None


def update_agent_success_rate(
    arg1: Any,
    arg2: Any,
    arg3: Any = None,
    *,
    db: Session | None = None,
) -> float | None:
    """Update an agent's success_rate using Exponential Moving Average (alpha=0.1).

    Formula: new_rate = 0.1 * outcome + 0.9 * old_rate
    Supports flexible argument calling conventions:
      - update_agent_success_rate(db, agent_id, outcome)
      - update_agent_success_rate(agent_id, outcome, db=...)
    """
    if isinstance(arg1, Session):
        session = arg1
        agent_id = str(arg2) if arg2 is not None else ""
        outcome = float(arg3) if arg3 is not None else 0.0
    else:
        agent_id = str(arg1) if arg1 is not None else ""
        outcome = float(arg2) if arg2 is not None else 0.0
        session = db if db is not None else arg3

    if not session or not agent_id:
        return None

    agent = session.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        return None

    old_rate = float(agent.success_rate) if agent.success_rate is not None else 0.0
    new_rate = 0.1 * outcome + 0.9 * old_rate
    agent.success_rate = new_rate
    session.add(agent)
    return new_rate



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


class TaskStateMachine:
    """Core state transitions, compare-and-set updates, and append-only gate ledger."""

    PATCHABLE_FIELDS = {"plan", "acceptance_criteria", "priority", "tags", "raw_input"}

    def __init__(self, db: Session):
        self.db = db
        self.validator = TaskValidator(db)
        self._deferred_landing_event: tuple[str, dict[str, Any]] | None = None

    def cas_status(self, task: Task, new_status: str) -> None:
        """Move ``task.status`` forward with a compare-and-set UPDATE."""
        self.db.flush()
        expected_status = task.status
        expected_version = task.version
        result = self.db.execute(
            update(Task)
            .where(
                Task.id == task.id,
                Task.status == expected_status,
                Task.version == expected_version,
            )
            .values(status=new_status, version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise TransitionConflictError(
                f"Task {task.id} status changed concurrently while "
                f"transitioning {expected_status!r} -> {new_status!r} "
                f"(expected version {expected_version})"
            )
        task.status = new_status
        task.version = expected_version + 1

    @staticmethod
    def _record_is_pass_verdict(record: GateRecord) -> bool:
        """Return whether an immutable ledger row proves an approved pass."""
        output = record.output_payload or {}
        return (
            record.gate_type == "verdict"
            and record.status == "approved"
            and (record.output_ref == "pass" or output.get("verdict") == "pass")
        )

    def require_approved_pass_verdict(
        self,
        task: Task,
        *,
        approval_record: GateRecord | None = None,
    ) -> GateRecord:
        """Enforce the single service-level invariant for completion/landing.

        ``approval_record`` supports the normal atomic transition: the new
        append-only verdict row is still pending in this transaction when the
        task projection moves to ``done``.  ``cas_status`` flushes that row
        before issuing its UPDATE, so the deferred PostgreSQL constraint sees
        the same proof at commit time.
        """
        candidates: list[GateRecord] = []
        if approval_record is not None:
            candidates.append(approval_record)
        candidates.extend(
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task.id,
                GateRecord.gate_type == "verdict",
                GateRecord.status == "approved",
            )
            .order_by(GateRecord.id.desc())
            .all()
        )
        approved = next(
            (
                record
                for record in candidates
                if record.task_id == task.id and self._record_is_pass_verdict(record)
            ),
            None,
        )
        if approved is None or (task.verdict or task.final_verdict) != "pass":
            raise PrerequisiteError(
                f"Task {task.id} has no approved pass verdict; completion and "
                "landing require an independently reviewed result."
            )
        self.validator.require_independent(task.executor, task.reviewer)
        if not task.result_ref or not task.result_ref.strip():
            raise PrerequisiteError("result_ref is required for completion")
        return approved

    def transition_to_done(
        self,
        task: Task,
        *,
        approval_record: GateRecord | None = None,
        now: datetime | None = None,
    ) -> None:
        """Project a passing verdict to ``done`` through one guarded path."""
        self.require_approved_pass_verdict(task, approval_record=approval_record)
        if task.status != "done":
            self.cas_status(task, "done")
        task.completed_at = now or datetime.now(timezone.utc)
        task.final_result_ref = task.result_ref
        task.final_verdict = "pass"
        task.awaiting_approval = False
        task.approval_prompt = None
        task.error = None

    def ledger_record(
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
        executor: str | None = None,
        reviewer: str | None = None,
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
            executor=executor if executor is not None else task.executor,
            reviewer=reviewer if reviewer is not None else task.reviewer,
            input_payload=payload,
            output_payload=output_payload,
            error_message=error_message,
        )
        self.db.add(record)
        return record

    def sync_awaiting_approval(self, task: Task) -> bool:
        """Recalculate the approval projection from unresolved gate records.

        Gate records are append-only: a pending record is resolved by an
        approved/rejected child rather than by updating the pending row.  A
        pending root with any decision child is therefore not an active gate.
        """
        self.db.flush()
        decision = aliased(GateRecord)
        has_pending = (
            self.db.query(GateRecord.id)
            .filter(
                GateRecord.task_id == task.id,
                GateRecord.status == "pending",
                ~exists().where(decision.parent_id == GateRecord.id),
            )
            .first()
            is not None
        )
        task.awaiting_approval = has_pending
        if not has_pending:
            task.approval_prompt = None
        return has_pending

    def _reject_pending_gates(
        self,
        task: Task,
        *,
        reason: str,
        actor: str,
        gate_type: str | None = None,
        idempotency_suffix: str,
    ) -> int:
        """Append rejection decisions for unresolved pending gate roots."""
        self.db.flush()
        decision = aliased(GateRecord)
        query = self.db.query(GateRecord).filter(
            GateRecord.task_id == task.id,
            GateRecord.status == "pending",
            ~exists().where(decision.parent_id == GateRecord.id),
        )
        if gate_type is not None:
            query = query.filter(GateRecord.gate_type == gate_type)

        rejected = 0
        for stale in query.all():
            record = self.ledger_record(
                task=task,
                gate_type=stale.gate_type,
                status="rejected",
                actor=actor,
                idempotency_key=f"{stale.idempotency_key}:{idempotency_suffix}",
                input_hash=stale.input_hash,
                payload=stale.input_payload or {},
                parent_id=stale.id,
                error_message=reason,
            )
            self.audit(task, record, reason=reason)
            rejected += 1
        return rejected

    def _reject_all_pending_gates(self, task: Task, reason: str) -> None:
        """Reject all unresolved gates when a task reaches a terminal state."""
        self._reject_pending_gates(
            task,
            reason=reason,
            actor="system:terminal-cleanup",
            idempotency_suffix="terminal",
        )
        self.sync_awaiting_approval(task)

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

    @staticmethod
    def gate_output(task: Task, gate_type: str) -> dict[str, Any]:
        return {
            "gate_type": gate_type,
            "task_status": task.status,
            "current_gate": task.current_gate,
            "executor": task.executor,
            "reviewer": task.reviewer,
            "result_ref": task.result_ref,
            "verdict": task.verdict,
        }

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

    def record_verdict_on_round(
        self,
        task: Task,
        *,
        verdict: str,
        now: datetime,
    ) -> None:
        if not task.current_round_id:
            return
        current_round = self.db.get(TaskRound, task.current_round_id)
        if current_round is None:
            return
        review_run = self.terminal_review_run(task.id)
        current_round.verdict = verdict
        current_round.findings_ref = task.findings
        current_round.reviewer_agent_id = task.reviewer
        current_round.reviewer_run_id = review_run.id if review_run else None
        current_round.result_ref = task.result_ref
        current_round.status = task.status
        current_round.completed_at = now

    def land_verdict_result(self, task: Task) -> LandingResult:
        project = self.db.get(Project, task.project) if task.project else None
        repo_root = getattr(project, "repo_root", None)
        head = head_of(task.result_ref)
        if not repo_root or not head:
            return LandingResult(ok=True, skipped_reason="no repo_root or head commit")
        return land_result(
            os.path.abspath(repo_root),
            head,
            f"Merge {task.id}: {task.title} (verdict pass, reviewer {task.reviewer})",
        )

    def terminal_review_run(self, task_id: str) -> AgentRun | None:
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

    def notify_gate_pending(self, task: Task, record: GateRecord) -> None:
        emit_task_event(
            task_id=task.id,
            event_type="gate_pending",
            kind="decision",
            payload={
                "gate": record.gate_type,
                "gate_record_id": record.id,
            },
            db=self.db,
        )

    def resolve_gate_notification(
        self,
        task_id: str,
        gate_type: str,
        gate_record_id: int,
        state: str,
    ) -> None:
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

    def wake_dependents(self, task_id: str) -> None:
        from app.workers.agent_runner import advance_task

        for dependent_id in self.validator.dependent_task_ids(task_id):
            try:
                advance_task.send(dependent_id, "dependency_closed")
            except Exception:
                logger.warning(
                    "Could not enqueue advance_task for dependent %s of %s",
                    dependent_id,
                    task_id,
                    exc_info=True,
                )

    def result_for_record(
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

    def apply_gate(
        self,
        task: Task,
        gate_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        approval_record: GateRecord | None = None,
    ) -> tuple[AgentRun | None, str | None]:
        self.validator.assert_status(task, str(payload["expected_status"]))
        now = datetime.now(timezone.utc)
        if gate_type == "dispatch":
            run_id = str(uuid.uuid4())
            kind = str(payload.get("kind", "execute"))
            agent_role = str(payload.get("agent_role", "executor"))
            if kind == "review":
                self.validator.require_independent(task.executor, str(payload["agent_id"]))
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
                idempotency_key=idempotency_key,
                dispatch_decision_id=payload.get("dispatch_decision_id"),
            )
            if kind == "execute" and task.status == "changes-requested":
                prior_head = self.prior_executor_head(task)
                if prior_head:
                    # The worker extracts the left side as the worktree base.
                    # Keeping a range-shaped ref preserves that existing
                    # execution path while making the prior head explicit.
                    run.result_ref = f"{prior_head}..{prior_head}"
            self.db.add(run)
            self.cas_status(task, "dispatched")
            task.current_gate = "dispatch"
            if kind == "review":
                task.reviewer = str(payload["agent_id"])
                run.task_round_id = task.current_round_id
            else:
                task.executor = str(payload["agent_id"])
                task_round = self.start_round(
                    task, agent_id=str(payload["agent_id"]), run_id=run_id, now=now
                )
                run.task_round_id = task_round.id
            task.dispatched_at = now
            task.error = None
            task.awaiting_approval = False
            task.approval_prompt = None
            record_run_requested(self.db, run, str(payload["repo_root"]))
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
            self.validator.require_independent(task.executor, reviewer)
            run_id = str(uuid.uuid4())
            prior_attempts = (
                self.db.query(func.coalesce(func.max(AgentRun.attempt), 0))
                .filter(
                    AgentRun.task_round_id == task.current_round_id,
                    AgentRun.kind == "review",
                )
                .scalar()
            )
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
                idempotency_key=idempotency_key,
                task_round_id=task.current_round_id,
                attempt=int(prior_attempts) + 1,
            )
            self.db.add(run)
            task.reviewer = reviewer
            self.cas_status(task, "in-review")
            task.current_gate = "verdict"
            task.error = None
            task.awaiting_approval = False
            task.approval_prompt = None
            record_run_requested(self.db, run, str(payload["repo_root"]))
            return run, run_id
        if gate_type == "verdict":
            verdict = str(payload["verdict"])
            self.validator.validate_verdict_prerequisites(
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
                # Landing is an external side effect.  Prove the immutable
                # approved verdict before touching git, not merely before the
                # final task projection update.
                self.require_approved_pass_verdict(
                    task, approval_record=approval_record
                )
                landing = self.land_verdict_result(task)
                if not landing.ok:
                    task.awaiting_approval = True
                    task.approval_prompt = (
                        f"Verdict is pass but landing {task.result_ref} failed: "
                        f"{landing.error} — fix the repo, then call land_task."
                    )
                    task.error = f"landing_failed: {landing.error}"
                    self.record_verdict_on_round(task, verdict=verdict, now=now)
                    self._deferred_landing_event = (
                        "landing_failed",
                        {"result_ref": task.result_ref, "error": landing.error},
                    )
                    self._update_verdict_agent_success_rates(task, verdict)
                    return None, verdict
                if landing.landed_ref:
                    task.landed_ref = landing.landed_ref
                    self._deferred_landing_event = (
                        "landed",
                        {
                            "landed_ref": landing.landed_ref,
                            "result_ref": task.result_ref,
                        },
                    )
                self.transition_to_done(
                    task, approval_record=approval_record, now=now
                )
            else:
                self.cas_status(task, "changes-requested")
                task.completed_at = None
            self.record_verdict_on_round(task, verdict=verdict, now=now)
            self._update_verdict_agent_success_rates(task, verdict)
            return None, verdict
        raise OrchestrationError(f"Unsupported gate type: {gate_type}")

    def _update_verdict_agent_success_rates(self, task: Task, verdict: str) -> None:
        outcome = 1.0 if verdict == "pass" else 0.0
        if task.executor:
            update_agent_success_rate(self.db, task.executor, outcome)
        if task.reviewer:
            update_agent_success_rate(self.db, task.reviewer, outcome)


    def request_gate(
        self,
        *,
        task: Task,
        gate_type: str,
        actor: str,
        idempotency_key: str,
        expected_status: str,
        payload: dict[str, Any],
    ) -> TransitionResult:
        self.validator.validate_common(task, actor, idempotency_key)
        request_payload = {
            **payload,
            "expected_status": expected_status,
            "gate_type": gate_type,
        }
        if gate_type == "review_order":
            reviewer = str(request_payload.get("reviewer") or "").strip()
            selection_reason = str(
                request_payload.get("selection_reason") or "not provided"
            ).strip()
            request_payload["approval_prompt"] = (
                f"Reviewer đề xuất: {reviewer} — lý do: "
                f"{selection_reason}. Approve?"
            )
        input_hash = TaskValidator.input_hash(request_payload)

        # Reject stale pending gates BEFORE idempotency check - a new request
        # should clean up orphaned gates even if this specific request is idempotent.
        # Must commit here because idempotency check may return early.
        effective_mode = self.validator.mode_for_task(task)
        if effective_mode == "supervised":
            rejected_count = self._reject_pending_gates(
                task,
                gate_type=gate_type,
                reason="Superseded by newer gate request",
                actor="system:stale-cleanup",
                idempotency_suffix="auto-rejected",
            )
            if rejected_count > 0:
                self.sync_awaiting_approval(task)
                self.db.commit()

        existing = self.validator.idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            self.validator.reject_if_stale_dispatch_record(existing)
            previous_awaiting = task.awaiting_approval
            previous_prompt = task.approval_prompt
            self.sync_awaiting_approval(task)
            if (
                task.awaiting_approval != previous_awaiting
                or task.approval_prompt != previous_prompt
            ):
                self.db.commit()
                self.db.refresh(task)
            return self.result_for_record(task, existing)
        self.validator.assert_status(task, expected_status)
        if gate_type == "dispatch":
            decision = self.validator.check_brakes(
                task,
                for_spawn=True,
                audit=True,
                agent_id=payload.get("brake_agent_id"),
            )
            if not decision.allowed and not decision.queue:
                raise BrakeViolationError(decision.reason or "Safety brake engaged")
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

        if effective_mode == "plan-only" and gate_type in {"dispatch", "verdict"}:
            record = self.ledger_record(
                task=task,
                gate_type=gate_type,
                status="rejected",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
                error_message="plan-only mode blocks this transition",
            )
            self.sync_awaiting_approval(task)
            self.audit(task, record, reason=record.error_message)
            self.db.commit()
            raise ModeViolationError(record.error_message)

        if effective_mode == "supervised":
            record = self.ledger_record(
                task=task,
                gate_type=gate_type,
                status="pending",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
            )
            task.awaiting_approval = True
            task.approval_prompt = request_payload.get("approval_prompt") or (
                f"Approve {gate_type} gate for task {task.id} "
                f"(request {idempotency_key})?"
            )
            self.sync_awaiting_approval(task)
            self.audit(task, record)
            self.db.commit()
            self.db.refresh(task)
            self.db.refresh(record)
            self.notify_gate_pending(task, record)
            return TransitionResult(task, record, False)

        record: GateRecord | None = None
        if gate_type == "verdict":
            verdict = str(request_payload["verdict"])
            record = self.ledger_record(
                task=task,
                gate_type=gate_type,
                status="approved",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
                output_ref=verdict,
                output_payload={"verdict": verdict},
            )
        run, output_ref = self.apply_gate(
            task,
            gate_type,
            request_payload,
            idempotency_key=idempotency_key,
            approval_record=record,
        )
        if record is None:
            record = self.ledger_record(
                task=task,
                gate_type=gate_type,
                status="approved",
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=request_payload,
                output_ref=output_ref,
                output_payload=self.gate_output(task, gate_type),
            )
        self._sync_after_transition(task)
        self.audit(task, record)
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
        findings: list[Any] | None = None,
        expected_status: str = "in-review",
    ) -> TransitionResult:
        task = self.validator.task(task_id)
        normalized_verdict = verdict.strip().lower()
        if normalized_verdict not in {"pass", "changes"}:
            raise PrerequisiteError("Verdict must be pass or changes")
        self.validator.validate_verdict_prerequisites(
            task,
            actor=actor,
            verdict=normalized_verdict,
            ac_results=ac_results,
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
        task = self.validator.task(pending.task_id)
        decision_payload = {
            "pending_id": pending.id,
            "decision": decision,
            "gate_type": pending.gate_type,
        }
        input_hash = TaskValidator.input_hash(decision_payload)
        existing = self.validator.idempotent_record(task.id, idempotency_key, input_hash)
        if existing is not None:
            previous_awaiting = task.awaiting_approval
            previous_prompt = task.approval_prompt
            self.sync_awaiting_approval(task)
            if (
                task.awaiting_approval != previous_awaiting
                or task.approval_prompt != previous_prompt
            ):
                self.db.commit()
                self.db.refresh(task)
            return self.result_for_record(task, existing)

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
        record: GateRecord | None = None
        self._deferred_landing_event = None
        if effective_decision == "approved":
            if pending.gate_type == "verdict":
                verdict = str((pending.input_payload or {})["verdict"])
                record = self.ledger_record(
                    task=task,
                    gate_type=pending.gate_type,
                    status=effective_decision,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    input_hash=input_hash,
                    payload=decision_payload,
                    output_ref=verdict,
                    output_payload={"verdict": verdict},
                    error_message=reason,
                    parent_id=pending.id,
                )
            run, output_ref = self.apply_gate(
                task,
                pending.gate_type,
                pending.input_payload or {},
                idempotency_key=pending.idempotency_key,
                approval_record=record,
            )
        elif pending.gate_type == "verdict":
            self.validator.assert_status(task, "in-review")
            self.cas_status(task, "awaiting-review")
            task.current_gate = "review_order"
            task.verdict = None
            task.completed_at = None
            task.awaiting_approval = False
            task.approval_prompt = None

        if record is None:
            record = self.ledger_record(
                task=task,
                gate_type=pending.gate_type,
                status=effective_decision,
                actor=actor,
                idempotency_key=idempotency_key,
                input_hash=input_hash,
                payload=decision_payload,
                output_ref=output_ref,
                output_payload=self.gate_output(task, pending.gate_type),
                error_message=reason,
                parent_id=pending.id,
            )
        self._sync_after_transition(task)
        self.audit(task, record, reason=reason)
        landing_event = getattr(self, "_deferred_landing_event", None)
        self._deferred_landing_event = None
        if landing_event is not None:
            emit_task_event(
                task_id=task.id,
                event_type=landing_event[0],
                payload=landing_event[1],
                db=self.db,
            )
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
        self.resolve_gate_notification(task.id, pending.gate_type, pending.id, effective_decision)
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

    def escalate_task(
        self, *, task_id: str, reason: str, actor: str = "system"
    ) -> GateRecord:
        task = self.validator.task(task_id)
        self.cas_status(task, "failed")
        task.error = reason
        task.awaiting_approval = True
        task.approval_prompt = reason
        task.updated_at = datetime.now(timezone.utc)
        payload = {"reason": reason, "task_status": "failed"}
        record = self.ledger_record(
            task=task,
            gate_type="escalation",
            status="rejected",
            actor=actor,
            idempotency_key=str(uuid.uuid4()),
            input_hash=TaskValidator.input_hash(payload),
            payload=payload,
            error_message=reason,
        )
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
        critic: str,
        critic_verdict: str,
        critic_findings: list[dict[str, Any]],
        critic_summary: str,
        critic_tokens: int,
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
        if critic_verdict not in {"accept", "reject"}:
            raise PrerequisiteError("critic_verdict must be accept or reject")
        if critic_verdict == "reject" and not critic_findings:
            raise PrerequisiteError("critic rejection requires evidenced findings")
        for finding in critic_findings:
            if not isinstance(finding, dict) or not finding.get("evidence"):
                raise PrerequisiteError(
                    "every critic rejection finding requires reproducible evidence"
                )
        self.validator.require_independent(planner, critic)

        task.acceptance_criteria = acceptance_criteria
        task.constraints = constraints
        task.evidence = evidence
        task.prior_art = prior_art
        task.ruled_out = ruled_out
        task.limits = limits
        task.planner = planner
        task.plan_critic = critic
        task.plan_critic_status = critic_verdict
        task.plan_critic_findings = critic_findings
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
        if critic_verdict == "reject":
            task.awaiting_approval = True
            task.approval_prompt = (
                f"Plan critic {critic} rejected this plan: {critic_summary}. "
                "Correct the evidenced findings and run generate_spec_plan again."
            )
        elif open_questions or spec_clarity != "high":
            questions = "\n".join(
                f"{index}) {question}" for index, question in enumerate(open_questions, 1)
            )
            task.awaiting_approval = True
            question_block = f"\n{questions}" if questions else ""
            task.approval_prompt = (
                f"Spec chưa đủ rõ (clarity={spec_clarity}). Trả lời các câu hỏi sau "
                f"rồi chạy lại generate_spec_plan:{question_block}"
            )
        else:
            task.open_questions = []
            task.awaiting_approval = False
            task.approval_prompt = None
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
        critic_payload = {
            "planner": planner,
            "critic": critic,
            "verdict": critic_verdict,
            "findings": critic_findings,
            "summary": critic_summary,
            "token_budget": 50_000,
            "tokens_used": critic_tokens,
            "diff_provided": False,
        }
        critic_record = self.ledger_record(
            task=task,
            gate_type="plan_critic",
            status="approved" if critic_verdict == "accept" else "rejected",
            actor=critic,
            idempotency_key=str(uuid.uuid4()),
            input_hash=TaskValidator.input_hash(critic_payload),
            payload=critic_payload,
            output_payload={
                "verdict": critic_verdict,
                "findings": critic_findings,
                "summary": critic_summary,
                "tokens_used": critic_tokens,
            },
            error_message=critic_summary if critic_verdict == "reject" else None,
            executor=planner,
            reviewer=critic,
        )
        self.audit(task, critic_record)
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
                    "critic": critic,
                    "critic_verdict": critic_verdict,
                    "critic_tokens": critic_tokens,
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

    def complete_no_commit_task(
        self, *, task_id: str, actor: str, run_id: str | None = None
    ) -> dict:
        task = self.validator.task(task_id)
        tags = [str(t).lower() for t in (task.tags or [])]
        if "no-commit" not in tags:
            raise PrerequisiteError(
                "RESULT_REF: none is only accepted for tasks tagged "
                "'no-commit'; commit your work or tag the task."
            )
        now = datetime.now(timezone.utc)
        payload = {
            "kind": "no_commit_completion",
            "run_id": run_id,
            "note": "read-only task: no diff to review, auto-pass recorded by system",
        }
        verdict_record = self.ledger_record(
            task=task,
            gate_type="verdict",
            status="approved",
            actor=actor,
            idempotency_key=f"no-commit:{task.id}:{run_id or 'manual'}",
            input_hash=TaskValidator.input_hash(payload),
            payload=payload,
            output_ref="pass",
            output_payload={"verdict": "pass"},
        )
        task.result_ref = "no-commit"
        task.verdict = "pass"
        if not task.reviewer:
            task.reviewer = "@system-no-commit"
        self.transition_to_done(task, approval_record=verdict_record, now=now)
        self._sync_after_transition(task)
        emit_task_event(
            task_id=task.id,
            event_type="done",
            payload={"no_commit": True, "run_id": run_id},
            db=self.db,
        )
        self.db.commit()
        return {"action": "no_commit_completed", "task_id": task.id, "status": task.status}

    def land_task(self, *, task_id: str, actor: str) -> dict:
        task = self.validator.task(task_id)
        approval_record = self.require_approved_pass_verdict(task)
        landing = self.land_verdict_result(task)
        if not landing.ok:
            task.awaiting_approval = True
            task.approval_prompt = (
                f"Landing {task.result_ref} failed: {landing.error} — "
                "fix the repo, then call land_task again."
            )
            task.error = f"landing_failed: {landing.error}"
            self.sync_awaiting_approval(task)
            self.db.commit()
            return {"action": "landing_failed", "task_id": task.id, "error": landing.error}

        now = datetime.now(timezone.utc)
        if task.status != "done":
            self.transition_to_done(task, approval_record=approval_record, now=now)
        task.awaiting_approval = False
        task.approval_prompt = None
        task.error = None
        self._sync_after_transition(task)
        if landing.landed_ref:
            task.landed_ref = landing.landed_ref
            emit_task_event(
                task_id=task.id,
                event_type="landed",
                payload={
                    "landed_ref": landing.landed_ref,
                    "result_ref": task.result_ref,
                    "actor": actor,
                },
                db=self.db,
            )
            if task.project:
                project = self.db.get(Project, task.project)
                if project and project.repo_root:
                    record_commit_event(
                        self.db,
                        project.id,
                        project.repo_root,
                        commit_sha=landing.landed_ref,
                    )
        self.db.commit()
        return {
            "action": "landed" if landing.landed_ref else "landing_skipped",
            "task_id": task.id,
            "status": task.status,
            "landed_ref": landing.landed_ref,
            "skipped_reason": landing.skipped_reason,
        }

    def _resolve_attach_range(self, repo_root: str, commit_ref: str) -> str:
        """Normalise an attach_result ref into the ``base..head`` form.

        ``request_review`` refuses anything that is not a committed
        ``base..head`` range, so storing a bare hash here produced a task stuck
        in ``awaiting-review`` forever (CTV2-1337). Accept both shapes:

        - ``"<base>..<head>"`` — validate both ends, keep the range
        - ``"<commit>"``       — derive ``base`` from the commit's first parent
        """

        def resolve(ref: str) -> str:
            rev = subprocess.run(
                ["git", "-C", repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if rev.returncode != 0:
                raise OrchestrationError(
                    f"Commit or ref '{ref}' does not exist in repository {repo_root}"
                )
            return rev.stdout.strip()

        if ".." in commit_ref:
            base_ref, _, head_ref = commit_ref.partition("..")
            if not base_ref.strip() or not head_ref.strip():
                raise OrchestrationError(
                    f"Invalid range '{commit_ref}': expected '<base>..<head>'"
                )
            return f"{resolve(base_ref.strip())[:12]}..{resolve(head_ref.strip())[:12]}"

        head = resolve(commit_ref)
        parent = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--verify", f"{head}^1^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if parent.returncode == 0:
            return f"{parent.stdout.strip()[:12]}..{head[:12]}"

        # Root commit: no parent to diff against. Git's empty tree is the
        # canonical base for "everything this commit introduced", and keeps the
        # range diffable. Computed rather than hard-coded so it stays correct
        # for SHA-256 repositories.
        empty_tree = subprocess.run(
            ["git", "-C", repo_root, "hash-object", "-t", "tree", "/dev/null"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if empty_tree.returncode != 0:
            raise OrchestrationError(
                f"Commit '{commit_ref}' is a root commit and the empty-tree base "
                f"could not be resolved in {repo_root}; pass '<base>..<head>'"
            )
        return f"{empty_tree.stdout.strip()[:12]}..{head[:12]}"

    def attach_result(
        self,
        *,
        task_id: str,
        commit: str,
        option: str = "request_review",
        actor: str = "system",
        idempotency_key: str | None = None,
    ) -> TransitionResult:
        task = self.validator.task(task_id)

        opt = (option or "request_review").strip().lower().replace("-", "_")
        if opt == "done":
            raise PrerequisiteError(
                "attach_result cannot mark a task done; submit the result for "
                "independent review with option 'request_review'"
            )
        if opt != "request_review":
            raise OrchestrationError(
                f"Invalid option '{option}': must be 'request_review'"
            )

        # An immediate replay may arrive after the first call already moved
        # dispatched -> awaiting-review.  Every other source state, especially
        # in-review, is rejected before any repository probing.
        if task.status not in {"dispatched", "awaiting-review"}:
            self.validator.assert_status(task, "dispatched")

        commit_ref = (commit or "").strip()
        if not commit_ref:
            raise OrchestrationError("commit is required")

        project = self.db.get(Project, task.project) if task.project else None
        repo_root = getattr(project, "repo_root", None)
        if repo_root and os.path.exists(repo_root):
            try:
                probe = subprocess.run(
                    ["git", "-C", repo_root, "rev-parse", "--git-dir"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if probe.returncode == 0:
                    commit_ref = self._resolve_attach_range(repo_root, commit_ref)
            except subprocess.TimeoutExpired:
                raise OrchestrationError(f"Git timeout validating commit '{commit_ref}'")
            except (OSError, subprocess.SubprocessError):
                pass

        idempotency_key = idempotency_key or f"attach_result:{task_id}:{commit_ref}:{opt}"
        payload = {
            "task_id": task_id,
            "commit": commit_ref,
            "option": opt,
        }
        input_hash = TaskValidator.input_hash(payload)
        existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self.result_for_record(task, existing)

        self.validator.assert_status(task, "dispatched")

        now = datetime.now(timezone.utc)
        task.result_ref = commit_ref
        task.current_gate = "review_order"
        task.error = None
        task.awaiting_approval = False
        task.approval_prompt = None
        self.cas_status(task, "awaiting-review")
        target_status = "awaiting-review"

        task.updated_at = now

        record = self.ledger_record(
            task=task,
            gate_type="attach_result",
            status="approved",
            actor=actor,
            idempotency_key=idempotency_key,
            input_hash=input_hash,
            payload=payload,
            output_ref=commit_ref,
            output_payload={"status": target_status, "result_ref": commit_ref, "option": opt},
        )
        self._sync_after_transition(task)
        self.audit(task, record, reason=f"Attached commit {commit_ref} with option {opt}")

        emit_task_event(
            task_id=task.id,
            event_type="attach_result",
            kind="info",
            payload={"commit": commit_ref, "option": opt, "status": target_status},
            db=self.db,
        )

        self.db.commit()
        self.db.refresh(task)
        return TransitionResult(task=task, gate_record=record, applied=True)
