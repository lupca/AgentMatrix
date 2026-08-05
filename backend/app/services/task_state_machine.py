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


# --- Gate briefs (CTV2-1393) -------------------------------------------------
#
# A gate used to hand the decider an idempotency key ("Approve dispatch gate
# for task CTV2-1389 (request chat:mcp-...:dispatch:7d913a...)?") -- nothing
# to actually decide with. `brief` is derived read-only from what is already
# in the ledger/task/run rows; it is never hand-written and never stored as
# the thing that made the decision (the append-only GateRecord row still is).


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


def _verdict_diffstat(db: Session, task: Task | None) -> str | None:
    if task is None or not task.result_ref or ".." not in task.result_ref:
        return None
    project = db.get(Project, task.project) if task.project else None
    repo_root = getattr(project, "repo_root", None)
    if not repo_root:
        return None
    try:
        proc = subprocess.run(
            ["git", "diff", "--stat", task.result_ref],
            cwd=os.path.abspath(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()[:2000] or None


def verdict_ac_checks(record: GateRecord) -> list[str]:
    """The per-AC checks worth running before approving a verdict gate.

    Reuses the reviewer's own `ac_results` (what it claims to have checked)
    rather than inventing a generic list -- these are the checks THIS
    decision actually needs.
    """
    payload = record.input_payload or {}
    checks: list[str] = []
    for ac in payload.get("ac_results") or []:
        if isinstance(ac, dict):
            checks.append(
                f"AC {ac.get('id', '?')} ({ac.get('status', '?')}): re-run "
                "whatever the reviewer says proves it"
            )
    return checks


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


def _split_result_range(result_ref: str) -> tuple[str | None, str | None]:
    """Expose the committed review range to the review gate/run context."""
    if ".." not in result_ref:
        return None, result_ref
    base, head = result_ref.split("..", 1)
    return base or None, head or None


def _review_finding_from_payload(review_cycle_id: str, finding: Any) -> ReviewFinding:
    """Build a ReviewFinding row from either a structured dict (the schema
    in app/schemas/task.py: id/severity/category/file/line/description) or a
    bare string (the legacy .ct/review-<task>.json / chat /verdict path)."""
    if isinstance(finding, dict):
        title = str(
            finding.get("description")
            or finding.get("title")
            or finding.get("category")
            or "finding"
        )
        file_ref = finding.get("file")
        line_ref = finding.get("line")
        detail = None
        if file_ref:
            detail = f"{file_ref}:{line_ref}" if line_ref is not None else str(file_ref)
        severity = finding.get("severity")
        return ReviewFinding(
            review_cycle_id=review_cycle_id,
            severity=str(severity) if severity else None,
            title=title,
            detail=detail,
            status="open",
        )
    return ReviewFinding(
        review_cycle_id=review_cycle_id,
        severity=None,
        title=str(finding),
        detail=None,
        status="open",
    )


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

    def _emit_task_done_event(self, task: Task, *, actor: str | None) -> None:
        """Emit the one Telegram-whitelisted completion event (CTV2-1400).

        Called from every path that lands a task in ``done`` so `task_done`
        always carries task id, who did it, and which commit.
        """
        emit_task_event(
            task_id=task.id,
            event_type="task_done",
            payload={
                "task_id": task.id,
                "title": task.title,
                "executor": task.executor,
                "reviewer": task.reviewer,
                "actor": actor,
                "commit": task.landed_ref or task.final_result_ref or task.result_ref,
            },
            db=self.db,
            kind="decision",
        )

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
        # Written BEFORE the status flip on purpose: the CHECK constraint
        # `ck_tasks_terminal_not_awaiting_approval` is evaluated on the very
        # UPDATE that sets `status='done'`, so the row has to already carry the
        # projection the new status implies.  `as_status` asks the derivation
        # for exactly that.
        self.sync_awaiting_approval(task, as_status="done")
        if task.status != "done":
            self.cas_status(task, "done")
        task.completed_at = now or datetime.now(timezone.utc)
        task.final_result_ref = task.result_ref
        task.final_verdict = "pass"
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

    def link_landed_task_specs(self, task: Task, landing: LandingResult) -> None:
        """Backfill derived spec history for either landing entry point."""
        if not landing.landed_ref or not task.project:
            return
        project = self.db.get(Project, task.project)
        repo_root = getattr(project, "repo_root", None)
        if not repo_root:
            return
        commit_ref = task.result_ref if head_of(task.result_ref) else landing.landed_ref
        link_task_to_changed_specs(
            self.db,
            task,
            os.path.abspath(repo_root),
            commit_ref,
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
            # the block without moving the task.  The `expected_status` assert
            # at the top already confirmed the task has not moved since the
            # escalation was raised, so work simply resumes from where it
            # stopped.
            task.error = None
            return None, None
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

    def review_gate_count(self, task_id: str, *, round_: int) -> int:
        """Count review_order requests already made for this task.

        Feeds the attempt number in the driver's idempotency key so a retry
        after a resolved attempt does not reuse a spent key -- see
        `_advance_awaiting_review`.  Counts request rows only (``parent_id IS
        NULL``); decision children are not attempts.
        """
        return int(
            self.db.query(func.count(GateRecord.id))
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.gate_type == "review_order",
                GateRecord.parent_id.is_(None),
            )
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
        self._emit_task_done_event(task, actor=actor)
        self.db.commit()
        return {"action": "no_commit_completed", "task_id": task.id, "status": task.status}

    def land_task(self, *, task_id: str, actor: str) -> dict:
        task = self.validator.task(task_id)
        approval_record = self.require_approved_pass_verdict(task)
        landing = self.land_verdict_result(task)
        if not landing.ok:
            # This used to assert the flag and then immediately call sync,
            # which derived it back to False from gates alone and threw the
            # prompt away -- the block this branch exists to create never
            # survived the same function.  The error prefix is now the
            # evidence the `landing` hold derives from.
            task.error = f"landing_failed: {landing.error}"
            self.sync_awaiting_approval(task)
            emit_task_event(
                task_id=task.id,
                event_type="landing_failed",
                payload={
                    "result_ref": task.result_ref,
                    "error": landing.error,
                    "why": f"land hỏng: {landing.error}",
                    "next": "áp diff tay hoặc rebase rồi gọi land_task lại",
                },
                db=self.db,
                kind="decision",
            )
            self.db.commit()
            return {"action": "landing_failed", "task_id": task.id, "error": landing.error}

        now = datetime.now(timezone.utc)
        if task.status != "done":
            self.transition_to_done(task, approval_record=approval_record, now=now)
        task.error = None
        self._sync_after_transition(task)
        if landing.landed_ref:
            task.landed_ref = landing.landed_ref
            self.link_landed_task_specs(task, landing)
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
        self._emit_task_done_event(task, actor=actor)
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
        external_executor: str | None = None,
    ) -> TransitionResult:
        """Record a finished result against a task.

        Normally the work came from an AgentRun this system dispatched, so the
        task is already 'dispatched'. `external_executor` covers the other
        case: the work was genuinely done outside AGMX -- by the coordinator
        itself, by its own subagents, by hand -- and only needs recording.

        Without it there was no way to record such work at all: attaching
        demanded 'dispatched', and reaching 'dispatched' meant firing an agent
        to redo work that was already finished (CTV2-1403). The record is the
        point of a task; making the record cost a duplicate run made the
        system fight its own purpose.

        Provenance stays honest -- the event says no AGMX run produced this --
        and four-eyes is untouched: the named executor becomes the task's
        executor, so the reviewer must still be someone else.
        """
        task = self.validator.task(task_id)
        external = (external_executor or "").strip() or None

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
        allowed_sources = {"dispatched", "awaiting-review"}
        if external:
            # Work done outside the system starts from where it actually is.
            allowed_sources |= {"todo", "changes-requested"}
        if task.status not in allowed_sources:
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
        if external:
            payload["external_executor"] = external
        input_hash = TaskValidator.input_hash(payload)
        existing = self.validator.idempotent_record(task_id, idempotency_key, input_hash)
        if existing is not None:
            return self.result_for_record(task, existing)

        if external:
            if task.status not in allowed_sources:
                self.validator.assert_status(task, "dispatched")
            task.executor = external
        else:
            self.validator.assert_status(task, "dispatched")

        now = datetime.now(timezone.utc)
        task.result_ref = commit_ref
        task.current_gate = "review_order"
        task.error = None
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
            payload={
                "commit": commit_ref,
                "option": opt,
                "status": target_status,
                # Say it plainly: nothing this system ran produced this diff.
                "provenance": "external" if external else "agent_run",
                "external_executor": external,
            },
            db=self.db,
        )

        self.db.commit()
        self.db.refresh(task)
        return TransitionResult(task=task, gate_record=record, applied=True)
