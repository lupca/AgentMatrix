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

class VerdictLandingMixin:
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

        def _update_verdict_agent_success_rates(self, task: Task, verdict: str) -> None:
            outcome = 1.0 if verdict == "pass" else 0.0
            if task.executor:
                update_agent_success_rate(self.db, task.executor, outcome)
            if task.reviewer:
                update_agent_success_rate(self.db, task.reviewer, outcome)

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
