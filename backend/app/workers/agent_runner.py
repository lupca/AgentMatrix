"""Dramatiq actor that executes CLI agents with durable state and streaming."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import dramatiq
import psutil
from dramatiq.middleware import CurrentMessage
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import (
    AGENT_EVENT_TYPES,
    AgentEvent,
    AgentOutputChunk,
    AgentRun,
    AuditLog,
    LLMUsage,
    RunResourceUsage,
    Task,
    TaskDependency,
    VendorRawEvent,
)
from app.schemas.task import ReviewResult
from app.services.agent_matcher import AgentMatcher
from app.services.command_builder import _is_review_task, review_result_path
from app.services.process_manager import (
    ProcessManager,
    ProcessResult,
    ProcessStatus,
    WorktreeManager,
    WorktreeUnsupportedError,
)
from app.services.review_criteria import merged_review_criteria
from app.services.task_event_service import emit_task_event
from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService
from app.services.tool_metrics import record_tool_metric
from app.workers import plan_executor, redis_broker
from app.workers.cli_executor import (
    WORKTREE_ENABLED, AgentExecutionError, _build_execution_result_ref, _cleanup_mcp_config,
    _cleanup_stale_process, _current_attempt, _emit_decision_event,
    _git_ref, _has_committed_diff, _has_uncommitted_changes,
    _is_ancestor, _nudge_driver, _parse_result_ref, _prepare_review_artifact,
    _record_run_resource_usage, _record_unexpected_failure, _review_read_only_git_env,
    _run_base_ref, _submit_review_verdict, _throttled_cancel_check, _update_task_status,
    _use_worktree, execute_agent_run, publish_line, publish_status,
)
from app.workers.output_parser import (
    OUTPUT_CHUNK_LINES, ReviewResultLoadError, _extract_explicit_result_ref,
    _findings_by_severity, _json_line, _next_agent_event_seq, _next_chunk_index,
    _record_agent_event, _record_vendor_output, _vendor_event, load_review_result,
    normalize_cli_event, parse_cli_output, parse_vendor_event,
)
from app.workers.output_streamer import (
    clear_cancel_request, get_channel, is_cancel_requested, redis_client,
)

logger = logging.getLogger(__name__)

# Re-exported for backward compatibility and test monkeypatching
__all__ = [
    "OUTPUT_CHUNK_LINES", "WORKTREE_ENABLED", "AgentExecutionError", "ReviewResultLoadError",
    "_ACTIONABLE_STATUSES", "AUTO_MAX_ROUNDS", "_build_execution_result_ref", "_cleanup_mcp_config",
    "_cleanup_stale_process", "_current_attempt", "_extract_explicit_result_ref",
    "_findings_by_severity", "_git_ref", "_has_committed_diff", "_has_uncommitted_changes",
    "_is_ancestor", "_json_line", "_next_agent_event_seq", "_next_chunk_index",
    "_parse_result_ref", "_prepare_review_artifact", "_record_agent_event",
    "_record_run_resource_usage", "_record_unexpected_failure", "_record_vendor_output",
    "_review_read_only_git_env", "_run_base_ref", "_submit_review_verdict",
    "_throttled_cancel_check", "_update_task_status", "_use_worktree", "_vendor_event",
    "advance_task", "execute_agent_run", "load_review_result", "normalize_cli_event",
    "parse_cli_output", "parse_vendor_event", "publish_line", "publish_status",
    "run_agent", "run_agent_dead_letter", "subprocess",
]

AUTO_MAX_ROUNDS = max(1, int(os.getenv("AUTO_MAX_ROUNDS", "3")))


@dramatiq.actor(
    broker=redis_broker,
    max_retries=0,
    time_limit=30_000,
)
def run_agent_dead_letter(dead_message: dict, retry_info: dict) -> str:
    """Callback for dramatiq's `Retries` middleware when retries are exhausted."""
    args = dead_message.get("args") or ()
    run_id = args[0] if args else None
    message_id = dead_message.get("message_id", "unknown")
    retries = retry_info.get("retries")
    max_retries = retry_info.get("max_retries")
    last_error = (dead_message.get("options") or {}).get("traceback") or "no traceback recorded"
    error = f"dead-lettered after {retries}/{max_retries} retries: {last_error}"[:4000]

    if not run_id:
        logger.warning(
            "run_agent dead-letter: message %s carries no run_id, discarding as orphan",
            message_id,
        )
        return "discarded_no_run_id"

    db: Session = SessionLocal()
    try:
        run = db.get(AgentRun, run_id)
        if run is None:
            logger.warning(
                "run_agent dead-letter: run %s not found, discarding as orphan", run_id
            )
            return "discarded_orphan"
        if run.status not in {"queued", "running"}:
            logger.info(
                "run_agent dead-letter: run %s already resolved (%s), discarding",
                run_id,
                run.status,
            )
            return "discarded_resolved"

        if plan_executor.is_plan_run(run):
            # A planner/critic run never moves Task.status (stays 'todo'
            # throughout planning), so record_dispatch_queue_failure below --
            # which asserts a dispatch-flow status -- would always raise and
            # leave the row silently stuck 'queued'/'running' forever.
            run.status = "failed"
            run.pid = None
            run.error_message = error
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return "handled"

        service = TaskOrchestrationService(db)
        try:
            service.record_dispatch_queue_failure(
                run_id=run_id,
                error=error,
                actor="system:dead-letter",
                idempotency_key=f"deadletter:{message_id}",
            )
        except OrchestrationError:
            logger.exception(
                "run_agent dead-letter: could not transition run %s / its task out "
                "of its in-flight status",
                run_id,
            )
        return "handled"
    finally:
        db.close()


@dramatiq.actor(
    broker=redis_broker,
    max_retries=3,
    min_backoff=30_000,
    max_backoff=300_000,
    time_limit=7_200_000,  # 2h hard limit; actual timeout from settings (agent_run_timeout_seconds)
    notify_shutdown=True,
    on_retry_exhausted="run_agent_dead_letter",
)
def run_agent(
    run_id: str,
    task_id: str,
    command: str,
    repo_root: str,
    timeout_seconds: int = 900,
) -> int | None:
    """Execute an agent and persist/stream its full lifecycle."""
    return execute_agent_run(
        run_id=run_id,
        task_id=task_id,
        command=command,
        repo_root=repo_root,
        timeout_seconds=timeout_seconds,
    )


_ACTIONABLE_STATUSES = {"todo", "awaiting-review", "changes-requested"}


@dramatiq.actor(
    broker=redis_broker,
    max_retries=0,
    time_limit=60_000,
)
def advance_task(task_id: str, trigger: str) -> str:
    """Event-driven, 0-token orchestration driver."""
    db: Session = SessionLocal()
    try:
        task = (
            db.query(Task)
            .filter(Task.id == task_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if task is None:
            still_exists = db.query(Task.id).filter(Task.id == task_id).scalar()
            if still_exists is None:
                logger.warning("advance_task: task %s not found", task_id)
                return "not_found"
            logger.info(
                "advance_task: task %s locked by a concurrent call, skipping",
                task_id,
            )
            return "skipped_locked"

        status_before = task.status
        service = TaskOrchestrationService(db)
        if task.awaiting_approval:
            outcome = "gate_pending"
        elif _advance_task_stalled(db, task_id, status_before):
            _escalate(
                db,
                task,
                f"advance_task made no progress after {AUTO_MAX_ROUNDS} calls "
                f"at status {status_before!r}; escalating instead of looping",
            )
            outcome = "escalated_stall"
        else:
            outcome = _advance_task_step(db, service, task)

        db.add(
            AuditLog(
                task_id=task_id,
                action=f"advance_task:{trigger}",
                actor="system:orchestration-driver",
                details={
                    "status_before": status_before,
                    "status_after": task.status,
                    "outcome": outcome,
                },
            )
        )
        db.commit()
        if status_before != "failed" and task.status == "failed":
            service.wake_dependents(task_id)
        logger.info(
            "advance_task: task %s trigger=%s %s -> %s (%s)",
            task_id,
            trigger,
            status_before,
            task.status,
            outcome,
        )
        return outcome
    finally:
        db.close()


# Rounds that were never allowed to try.  A round blocked by a pending gate,
# or one that did nothing but raise the escalation itself, says nothing about
# whether the task can progress -- counting them turns the guard into a loop:
# escalate -> a human approves -> the driver runs again, sees the blocked
# rounds it just caused, and escalates on the spot.  CTV2-1389 went round that
# loop twice on 2026-08-05 with a finished, critic-accepted plan in hand.
_NOT_EVIDENCE_OF_STALL = {"gate_pending", "escalated_stall"}


def _advance_task_stalled(db: Session, task_id: str, current_status: str) -> bool:
    if current_status not in _ACTIONABLE_STATUSES:
        return False
    recent = [
        entry
        for entry in (
            db.query(AuditLog)
            .filter(AuditLog.task_id == task_id, AuditLog.action.like("advance_task:%"))
            .order_by(AuditLog.id.desc())
            .limit(AUTO_MAX_ROUNDS * 4)
            .all()
        )
        if isinstance(entry.details, dict)
        and entry.details.get("outcome") not in _NOT_EVIDENCE_OF_STALL
    ][:AUTO_MAX_ROUNDS]
    if len(recent) < AUTO_MAX_ROUNDS:
        return False
    return all(
        entry.details.get("status_before") == current_status
        and entry.details.get("status_after") == current_status
        for entry in recent
    )


def _escalate(db: Session, task: Task, reason: str) -> None:
    TaskOrchestrationService(db).escalate_task(task_id=task.id, reason=reason)
    _emit_decision_event(
        db,
        task_id=task.id,
        event_type="escalated",
        payload={"reason": reason},
    )


def _advance_task_step(
    db: Session,
    service: TaskOrchestrationService,
    task: Task,
) -> str:
    if task.awaiting_approval:
        return "gate_pending"
    if task.status == "todo":
        return _advance_todo(db, service, task)
    if task.status == "awaiting-review":
        return _advance_awaiting_review(db, service, task)
    if task.status == "changes-requested":
        return _advance_changes_requested(db, service, task)
    if task.status in {"dispatched", "in-review"}:
        return "waiting"
    if task.status in {"done", "failed"}:
        return "terminal"
    return "unhandled_status"


def _advance_todo(db: Session, service: TaskOrchestrationService, task: Task) -> str:
    if not merged_review_criteria(task.acceptance_criteria, task.constraints) and not task.legacy_no_ac:
        _escalate(
            db,
            task,
            "todo task has no acceptance criteria or constraints; refusing to dispatch (fail-closed)",
        )
        return "escalated_missing_ac"
    if task.planner and task.plan_critic_status != "accept":
        # Not having a critic verdict *yet* is a transient condition, not a
        # dead task: a critic run is usually in flight, or the coordinator is
        # about to start one with critique_spec_plan.  Escalating here marks
        # the task `failed`, which is terminal -- it cancels every active run
        # and rejects every pending gate, so the critic that was about to
        # answer gets killed and can never be re-run.
        #
        # CTV2-1388, 2026-08-05: this fired on the task filed to fix exactly
        # this class of bug, seconds after its plan was generated. The defect
        # killed its own fix, and there was no way back.
        #
        # Just decline to dispatch and say why.  advance_task is re-driven on
        # every state change, so it will dispatch as soon as a critic accepts;
        # a plan that genuinely never gets one still cannot dispatch, which is
        # the property this guard exists to preserve.
        logger.info(
            "advance_task: task %s waiting for an independent plan critic "
            "(plan_critic_status=%r); not dispatching",
            task.id,
            task.plan_critic_status,
        )
        return "waiting_plan_critic"

    brake = service.check_brakes(task, for_spawn=False, audit=True)
    if not brake.allowed:
        if brake.code == "dependency_pending":
            return "waiting_dependency"
        return f"brake:{brake.code}"

    return _dispatch_execute(db, service, task)


def _blocked_by_dependencies(db: Session, task: Task) -> str | None:
    dep_ids = [
        row.depends_on_task_id
        for row in db.query(TaskDependency.depends_on_task_id)
        .filter(TaskDependency.task_id == task.id)
        .all()
    ]
    if not dep_ids:
        return None

    by_id = {row.id: row for row in db.query(Task).filter(Task.id.in_(dep_ids)).all()}
    failed = sorted(
        dep_id for dep_id in dep_ids if by_id.get(dep_id) is None or by_id[dep_id].status == "failed"
    )
    if failed:
        _escalate(
            db,
            task,
            f"dependency task(s) failed or are missing, so this task can "
            f"never dispatch: {', '.join(failed)}",
        )
        return "escalated_dependency_failed"

    if any(by_id[dep_id].status != "done" for dep_id in dep_ids):
        return "waiting_dependency"
    return None


def _dispatch_execute(db: Session, service: TaskOrchestrationService, task: Task) -> str:
    blocked = _blocked_by_dependencies(db, task)
    if blocked is not None:
        return blocked

    agent_id = task.executor
    if not agent_id:
        suggestions = AgentMatcher(db).suggest_agents(task, top_n=1)
        agent_id = suggestions[0].agent_id if suggestions else None
    if not agent_id:
        _escalate(db, task, "no available executor agent found for dispatch")
        return "escalated_no_agent"

    round_ = service.changes_round_count(task.id)
    try:
        result = service.request_dispatch(
            task_id=task.id,
            agent_id=agent_id,
            actor="system:orchestration-driver",
            idempotency_key=f"advance:{task.id}:dispatch:r{round_}",
        )
    except OrchestrationError as exc:
        _escalate(db, task, f"dispatch failed: {exc}")
        return "escalated_dispatch_error"

    if not result.applied:
        return "gate_pending"
    run = result.agent_run
    if run is None:
        return "dispatch_no_run"
    _enqueue_run(service, run, result.context or {}, task_id=task.id)
    return "dispatched"


def _advance_awaiting_review(
    db: Session,
    service: TaskOrchestrationService,
    task: Task,
) -> str:
    if not task.result_ref or not task.result_ref.strip():
        return "waiting_result_ref"

    brake = service.check_brakes(task, for_spawn=False, audit=True)
    if not brake.allowed:
        return f"brake:{brake.code}"

    scoring = AgentMatcher(db).score_candidates(
        task, top_n=3, exclude_agent_id=task.executor
    )
    if not scoring.suggestions:
        _escalate(db, task, f"no independent reviewer available for task {task.id}")
        return "escalated_no_reviewer"
    reviewer = scoring.suggestions[0].agent_id
    selected = next(
        candidate
        for candidate in scoring.candidates
        if candidate.agent_id == reviewer
    )
    matched = ", ".join(selected.matched_skills) or "no keyword overlap"
    exclusions: list[str] = []
    for candidate in scoring.candidates:
        rejection = (candidate.rejection_reason or "").lower()
        if "four-eyes" in rejection:
            exclusions.append(f"{candidate.agent_id} (four-eyes)")
        elif "status is disabled" in rejection:
            exclusions.append(f"{candidate.agent_id} (disabled)")
    exclusion_text = ", ".join(exclusions) or "none"
    selection_reason = (
        f"{reviewer} selected by matcher: score={selected.final_score:.2f}, "
        f"capability match={matched}, success_rate={selected.performance:.0%}; "
        f"excluded={exclusion_text}"
    )

    round_ = service.changes_round_count(task.id)
    # Each attempt gets its own key.
    #
    # `(task_id, idempotency_key)` is UNIQUE, so a key is spent the moment its
    # request is stored -- nothing can rewrite the hash behind it.  Keying only
    # on task+round+reviewer meant a retry after a failed attempt reused a spent
    # key and raised IdempotencyConflictError forever.
    #
    # CTV2-1389, 2026-08-05: the stored hash covered `selection_reason`, which
    # embeds live `success_rate` telemetry.  Once those numbers moved, every
    # driver pass hit the conflict, escalated, was cleared by a human, and
    # escalated again on the next pass -- a closed loop, while the task held a
    # finished commit waiting to be reviewed.
    #
    # A retry after a resolved attempt IS a different request, so it says so.
    attempt = service.review_gate_count(task.id, round_=round_)
    try:
        result = service.request_review(
            task_id=task.id,
            reviewer=reviewer,
            actor="system:orchestration-driver",
            idempotency_key=(
                f"advance:{task.id}:review:r{round_}:a{attempt}:reviewer:{reviewer}"
            ),
            selection_reason=selection_reason,
        )
    except OrchestrationError as exc:
        _escalate(db, task, f"review request failed: {exc}")
        return "escalated_review_error"

    if not result.applied:
        return "gate_pending"
    run = result.agent_run
    if run is None:
        return "review_no_run"
    _enqueue_run(service, run, result.context or {}, task_id=task.id)
    return "review_requested"


def _advance_changes_requested(
    db: Session,
    service: TaskOrchestrationService,
    task: Task,
) -> str:
    round_ = service.changes_round_count(task.id)
    max_rounds = service.resolve_autonomy(task.project).auto_max_rounds
    plan_limit = (task.limits or {}).get("max_execution_rounds")
    if isinstance(plan_limit, int) and plan_limit > 0:
        max_rounds = min(max_rounds, plan_limit)
    if round_ >= max_rounds:
        _escalate(
            db,
            task,
            f"changes-requested round limit reached ({round_}/{max_rounds}); "
            "escalating for a human replan",
        )
        return "escalated_round_limit"

    brake = service.check_brakes(task, for_spawn=False, audit=True)
    if not brake.allowed:
        return f"brake:{brake.code}"

    try:
        service.reopen_for_replan(
            task_id=task.id,
            actor="system:orchestration-driver",
            idempotency_key=f"advance:{task.id}:replan:r{round_}",
        )
    except OrchestrationError as exc:
        _escalate(db, task, f"reopen for replan failed: {exc}")
        return "escalated_replan_error"

    return _dispatch_execute(db, service, task)


def _enqueue_run(
    service: TaskOrchestrationService,
    run: AgentRun,
    context: dict,
    *,
    task_id: str,
) -> None:
    try:
        message = run_agent.send(
            run.id,
            run.task_id,
            run.command,
            str(context["repo_root"]),
            run.timeout_seconds,
        )
        message_id = getattr(message, "message_id", None)
        if message_id:
            run.dramatiq_message_id = str(message_id)
            service.db.commit()
    except Exception as exc:
        error = f"Could not queue run: {exc}"
        service.record_dispatch_queue_failure(
            run_id=run.id,
            error=error,
            actor="system:orchestration-driver",
            idempotency_key=f"advance:{task_id}:queue-failure:{run.id}",
        )
