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

from app.services.fsm.verdict_landing import _verdict_diffstat, _split_result_range, TransitionResult, _review_finding_from_payload
from app.services.fsm.task_lifecycle import _is_cheap_executor


def _task_cost_and_tokens(db: Session, task_id: str) -> tuple[float, int]:
    cost = (
        db.query(func.coalesce(func.sum(LLMUsage.cost_usd), 0))
        .filter(LLMUsage.task_id == task_id)
        .scalar()
    )
    tokens = (
        db.query(
            func.coalesce(
                func.sum(LLMUsage.input_tokens + LLMUsage.output_tokens), 0
            )
        )
        .filter(LLMUsage.task_id == task_id)
        .scalar()
    )
    return float(cost or 0), int(tokens or 0)

def gate_unknowns(db: Session, task: Task | None) -> list[str]:
    """What the system knows it cannot answer for this task.

    Required whenever the task carries no spec_task_link at all: without a
    spec_item/impl_design anchor there is no basis to say whether the result
    matches intent. Does not block task creation -- it is disclosure, not a
    gate.
    """
    if task is None:
        return []
    has_link = (
        db.query(SpecTaskLink.id).filter(SpecTaskLink.task_id == task.id).first()
        is not None
    )
    if has_link:
        return []
    return [
        "task này không gắn spec_item/impl_design — không có căn cứ để nói kết "
        "quả có đúng ý định hay không"
    ]

def build_gate_brief(db: Session, record: GateRecord) -> dict[str, Any]:
    """Derive a human-readable brief for one gate record from live state.

    Every field comes from data already on the record/task/run -- nothing
    here is free text a caller supplied. Per gate_type:
      dispatch: chosen executor + why (score, who was passed over), plan
        summary, AC count, cost/tokens spent so far, risk.
      review_order: chosen reviewer + why, four-eyes check.
      verdict: verdict, each AC with its evidence, findings by severity,
        git diff --stat.
      escalation: what blocked it and what would clear it.
      safety_brake: the numbers against the limit, whether a result exists.
    """
    task = db.get(Task, record.task_id)
    payload = record.input_payload or {}
    gate_type = record.gate_type
    unknowns = gate_unknowns(db, task)

    if gate_type == "dispatch":
        cost, tokens = _task_cost_and_tokens(db, record.task_id)
        agent_id = payload.get("agent_id")
        kind = payload.get("kind", "execute")
        decision_id = payload.get("dispatch_decision_id")
        chosen_score = None
        passed_over: list[str] = []
        if decision_id:
            decision = db.get(DispatchDecision, decision_id)
            if decision is not None:
                for candidate in decision.candidates:
                    if candidate.agent_id == agent_id:
                        chosen_score = candidate.final_score
                        continue
                    if candidate.eligible:
                        passed_over.append(
                            f"{candidate.agent_id} (score={candidate.final_score})"
                        )
                    else:
                        passed_over.append(
                            f"{candidate.agent_id} (loại: "
                            f"{candidate.rejection_reason or 'không đủ điều kiện'})"
                        )
        ac_count = len(task.acceptance_criteria or []) if task else 0
        plan_summary = ((task.plan or "").strip()[:280]) if task else ""
        summary = (
            f"Executor: {agent_id} ({kind})"
            + (f", score={chosen_score}" if chosen_score is not None else "")
            + (
                f"; ứng viên bị loại: {', '.join(passed_over)}"
                if passed_over
                else "; không có ứng viên khác"
            )
            + f". Plan: {plan_summary or '(chưa có plan)'}"
            + f". Số AC: {ac_count}."
            + f" Đã tiêu: ${cost:.4f} / {tokens} tokens."
            + (f" Risk: {task.risk}." if task and task.risk else "")
        )
    elif gate_type == "review_order":
        reviewer = payload.get("reviewer")
        reason = payload.get("selection_reason") or "không nêu"
        executor = task.executor if task else None
        four_eyes_ok = bool(executor and reviewer and executor != reviewer)
        summary = (
            f"Reviewer: {reviewer} — lý do: {reason}. "
            f"Four-eyes (executor={executor} != reviewer={reviewer}): "
            + ("OK" if four_eyes_ok else "VI PHẠM")
            + "."
        )
    elif gate_type == "verdict":
        ac_results = payload.get("ac_results") or []
        findings = payload.get("findings") or []
        ac_lines = [
            f"{ac.get('id', '?')}: {ac.get('status', '?')} — "
            f"{ac.get('evidence') or 'không có bằng chứng'}"
            for ac in ac_results
            if isinstance(ac, dict)
        ]
        by_severity: dict[str, int] = {}
        for finding in findings:
            if isinstance(finding, dict):
                sev = str(finding.get("severity", "unknown"))
                by_severity[sev] = by_severity.get(sev, 0) + 1
        diffstat = _verdict_diffstat(db, task)
        summary = (
            f"Reviewer {payload.get('reviewer')} chấm: {payload.get('verdict')}. "
            f"AC ({len(ac_results)}): " + ("; ".join(ac_lines) or "(không có)") + ". "
            "Findings: "
            + (", ".join(f"{k}={v}" for k, v in by_severity.items()) or "none")
            + "."
            + (f"\ngit diff --stat:\n{diffstat}" if diffstat else "")
        )
    elif gate_type == "escalation":
        blocker = (task.approval_prompt if task else None) or payload.get(
            "reason"
        ) or record.error_message or "(không rõ)"
        summary = (
            f"Vướng: {blocker}. Gỡ được bằng cách xử lý nguyên nhân trên rồi gọi "
            "lại tool đã bị chặn (get_status cho biết tool nào)."
        )
    elif gate_type == "safety_brake":
        code = payload.get("code")
        reason = payload.get("reason")
        delivered = payload.get("result_delivered")
        summary = (
            f"Brake {code}: {reason}. Đã có result_ref: "
            + ("có" if delivered else "chưa")
            + "."
        )
    else:
        summary = record.error_message or f"Gate {gate_type} (task {record.task_id})"

    return {"summary": summary, "unknowns": unknowns}

class GateLedgerMixin:
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

        def record_gate_evidence(
            self, gate_record_id: int, evidence: list[dict[str, Any]]
        ) -> GateRecord:
            """Attach the checks a decider actually ran to the decision.

            GateRecord enforces append-only at the DB level (a `before_update`
            listener rejects any UPDATE, not just a status/parent change) -- so
            this cannot rewrite the decision row's `output_payload` in place. It
            appends a new child row instead, pointing at the decision via
            `parent_id`, carrying the same idempotency/mode/executor/reviewer
            lineage. Read it back via that `parent_id` link, not by re-reading
            the decision row.
            """
            decision = self.db.get(GateRecord, gate_record_id)
            if decision is None:
                raise TaskNotFoundError(f"Gate record {gate_record_id} not found")
            record = GateRecord(
                task_id=decision.task_id,
                gate_type="verdict_evidence",
                status="approved",
                actor=decision.actor,
                mode=decision.mode,
                idempotency_key=f"{decision.idempotency_key}:evidence",
                input_hash=TaskValidator.input_hash({"evidence": evidence}),
                parent_id=decision.id,
                executor=decision.executor,
                reviewer=decision.reviewer,
                input_payload={"evidence": evidence},
                output_payload={"evidence": evidence},
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return record

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
            if gate_type != "escalation":
                # Escalations are exempt on purpose (CTV2-1406).
                #
                # Every other gate authorises a *transition*, so it must start
                # from the status it was requested against.  An escalation
                # authorises nothing: it means "a human looked at this, stop
                # blocking".  Asserting its old status turns the good case into a
                # deadlock -- if the task moved forward on its own (`attach_result`
                # raising `todo` to `awaiting-review`), that is the block being
                # resolved, not a conflict, yet the assert rejects the approval and
                # the still-pending gate keeps `request_review` refused.  Gate
                # blocks review, status blocks gate, no tool escapes.
                #
                # Measured 2026-08-06: UIKI-001/003/004/005/006/008/010 -- seven
                # tasks, each with a commit attached, each stuck exactly here.
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
                # The projection is not cleared here: the decision child that
                # resolves this pending gate is only appended after `_apply_gate`
                # returns, so `_sync_after_transition` at the end of `decide_gate`
                # is the first moment the ledger can answer truthfully.
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
                record_run_requested(self.db, run, str(payload["repo_root"]))
                self.db.add(
                    ReviewCycle(
                        task_id=task.id,
                        task_round_id=task.current_round_id,
                        reviewer_id=reviewer,
                        reviewer_agent_run_id=run_id,
                        status="requested",
                    )
                )
                return run, run_id
            if gate_type == "verdict":
                verdict = str(payload["verdict"])
                review_cycle_id = payload.get("review_cycle_id")
                review_cycle = self.validator.validate_verdict_prerequisites(
                    task,
                    actor=str(payload["reviewer"]),
                    verdict=verdict,
                    ac_results=payload["ac_results"],
                    review_cycle_id=review_cycle_id,
                )
                now_ts = datetime.now(timezone.utc)
                review_cycle.status = verdict
                review_cycle.verdict = verdict
                review_cycle.submitted_at = review_cycle.submitted_at or now_ts
                review_cycle.completed_at = now_ts
                for finding in payload.get("findings") or []:
                    self.db.add(_review_finding_from_payload(review_cycle.id, finding))
                task.verdict = verdict
                task.findings = payload.get("findings") or []
                task.current_gate = "verdict"
                if verdict == "pass":
                    # Landing is an external side effect.  Prove the immutable
                    # approved verdict before touching git, not merely before the
                    # final task projection update.
                    self.require_approved_pass_verdict(
                        task, approval_record=approval_record
                    )
                    landing = self.land_verdict_result(task)
                    if not landing.ok:
                        # The error string IS the evidence: the `landing` hold is
                        # derived from this prefix, so there is no separate flag to
                        # keep in step with it.
                        task.error = f"landing_failed: {landing.error}"
                        self.record_verdict_on_round(task, verdict=verdict, now=now)
                        self._deferred_landing_event = (
                            "landing_failed",
                            {
                                "result_ref": task.result_ref,
                                "error": landing.error,
                                "why": f"land hỏng: {landing.error}",
                                "next": "áp diff tay hoặc rebase rồi gọi land_task lại",
                            },
                        )
                        self._update_verdict_agent_success_rates(task, verdict)
                        return None, verdict
                    if landing.landed_ref:
                        task.landed_ref = landing.landed_ref
                        self.link_landed_task_specs(task, landing)
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
            if gate_type == "escalation":
                # Approving an escalation means "someone looked at this"; it clears
                # the block without moving the task, so work resumes from wherever
                # the task actually is now -- which is not necessarily where it was
                # when the escalation was raised (see the `expected_status`
                # exemption at the top of this method).
                task.error = None
                return None, None
            raise OrchestrationError(f"Unsupported gate type: {gate_type}")

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
            # Hash the DECISION, not the prose explaining it.
            #
            # `selection_reason` (and the `approval_prompt` derived from it) embeds
            # live telemetry: "score=0.89, success_rate=100%".  Those numbers move
            # every time any agent finishes a task, so the same logical request --
            # same task, same round, same reviewer -- hashed differently on each
            # retry and collided with its own stored idempotency key:
            #
            #   review request failed: Idempotency key
            #   'advance:CTV2-1389:review:r1:reviewer:@claude-sonnet-high'
            #   was reused with different input
            #
            # CTV2-1389, 2026-08-05: that escalated, was cleared, escalated again on
            # the very next driver pass -- a loop with no exit, while the task held
            # a finished commit waiting to be reviewed.  Both fields stay in the
            # payload (they are what a human reads); they just do not decide
            # whether two requests are the same request.
            _EXPLANATORY_FIELDS = ("selection_reason", "approval_prompt")
            input_hash = TaskValidator.input_hash(
                {
                    key: value
                    for key, value in request_payload.items()
                    if key not in _EXPLANATORY_FIELDS
                }
            )

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
            elif gate_type == "review_order":
                # A delivered result is still protected by the same budget and
                # autonomy brakes as an execution dispatch.  Without this check,
                # a caller could bypass the brake by requesting a reviewer
                # directly: ``request_review`` creates an AgentRun through this
                # gate, even though ``check_brakes`` had already said no more
                # spending was allowed.
                decision = self.validator.check_brakes(
                    task,
                    for_spawn=True,
                    audit=True,
                    agent_id=payload.get("reviewer"),
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
                # The pending record just written IS the evidence; the prompt is
                # read back out of it (`input_payload.approval_prompt`) rather
                # than written twice into two places that can disagree.
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
                    kind="decision" if landing_event[0] == "landing_failed" else None,
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
                self._emit_task_done_event(task, actor=actor)
                self.db.commit()
                self.wake_dependents(task.id)
            return TransitionResult(
                task=task,
                gate_record=record,
                applied=effective_decision == "approved",
                agent_run=run,
                context=(pending.input_payload or {}) if run is not None else None,
            )

        def request_gate_count(self, task_id: str, *, gate_type: str) -> int:
            """Count gate *requests* of one type for a task.

            Feeds the attempt number in the driver's idempotency keys so a retry
            after a resolved attempt does not reuse a spent key.  Counts request
            rows only (``parent_id IS NULL``); decision children are not attempts.
            """
            return int(
                self.db.query(func.count(GateRecord.id))
                .filter(
                    GateRecord.task_id == task_id,
                    GateRecord.gate_type == gate_type,
                    GateRecord.parent_id.is_(None),
                )
                .scalar()
                or 0
            )

        def review_gate_count(self, task_id: str, *, round_: int) -> int:
            """Attempt counter for the review leg -- see `_advance_awaiting_review`."""
            return self.request_gate_count(task_id, gate_type="review_order")

        def dispatch_gate_count(self, task_id: str) -> int:
            """Attempt counter for the dispatch leg -- see `_dispatch_execute`."""
            return self.request_gate_count(task_id, gate_type="dispatch")
