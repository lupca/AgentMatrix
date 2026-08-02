"""Dramatiq actor that executes CLI agents with durable state and streaming."""

from __future__ import annotations

import asyncio
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
    Session as SessionModel,
    Task,
    TaskDependency,
    TaskEvent,
    VendorRawEvent,
)
from app.schemas.task import ReviewResult
from app.services.agent_matcher import AgentMatcher
from app.services.command_builder import _is_review_task, review_result_path
from app.services.coordinator import CoordinatorService
from app.services.process_manager import (
    ProcessManager,
    ProcessResult,
    ProcessStatus,
    WorktreeManager,
    WorktreeUnsupportedError,
)
from app.services.task_event_service import TaskEventService, emit_task_event
from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService
from app.services.tool_metrics import record_tool_metric
from app.workers import redis_broker
from app.workers.cli_executor import (
    WORKTREE_ENABLED, AgentExecutionError, _build_execution_result_ref, _cleanup_mcp_config,
    _cleanup_stale_process, _current_attempt, _decision_status_message, _emit_decision_event,
    _enqueue_coordinator_wake, _git_ref, _has_committed_diff, _has_uncommitted_changes,
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
    "run_agent", "run_agent_dead_letter", "subprocess", "wake_coordinator",
]

AUTO_MAX_ROUNDS = max(1, int(os.getenv("AUTO_MAX_ROUNDS", "3")))


@dramatiq.actor(
    broker=redis_broker,
    max_retries=0,
    time_limit=900_000,
)
def wake_coordinator(event_id: int) -> str:
    """Claim one decision event and wake exactly one coordinator session."""
    db: Session = SessionLocal()
    try:
        event = db.get(TaskEvent, event_id)
        if event is None:
            return "not_found"
        if event.kind != "decision":
            return "ignored_info"

        task = db.get(Task, event.task_id)
        session = None
        if task is not None and task.session_id:
            session = (
                db.query(SessionModel)
                .filter(
                    SessionModel.id == task.session_id,
                    SessionModel.status == "active",
                )
                .first()
            )
        if session is None:
            session = (
                db.query(SessionModel)
                .filter(
                    SessionModel.context_level == "global",
                    SessionModel.status == "active",
                )
                .order_by(
                    SessionModel.last_activity_at.desc(),
                    SessionModel.id.desc(),
                )
                .first()
            )
        if session is None:
            return "parked"

        if not TaskEventService(db).claim_event(event.id, session.id):
            return "already_claimed"

        message = _decision_status_message(event)
        asyncio.run(
            CoordinatorService(db).run_turn_programmatic(
                session,
                message,
                source_event_id=event.id,
            )
        )
        return "completed"
    finally:
        db.close()


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
        was_awaiting_approval = bool(task.awaiting_approval)
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
        if outcome == "gate_pending" and not was_awaiting_approval:
            gate_event = (
                db.query(TaskEvent)
                .filter(
                    TaskEvent.task_id == task_id,
                    TaskEvent.kind == "decision",
                    TaskEvent.event_type == "gate_pending",
                    TaskEvent.claimed_by_session_id.is_(None),
                )
                .order_by(TaskEvent.id.desc())
                .first()
            )
            if gate_event is not None:
                _enqueue_coordinator_wake(gate_event.id)
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


def _advance_task_stalled(db: Session, task_id: str, current_status: str) -> bool:
    if current_status not in _ACTIONABLE_STATUSES:
        return False
    recent = (
        db.query(AuditLog)
        .filter(AuditLog.task_id == task_id, AuditLog.action.like("advance_task:%"))
        .order_by(AuditLog.id.desc())
        .limit(AUTO_MAX_ROUNDS)
        .all()
    )
    if len(recent) < AUTO_MAX_ROUNDS:
        return False
    return all(
        isinstance(entry.details, dict)
        and entry.details.get("status_before") == current_status
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
    if not (task.acceptance_criteria or []) and not task.legacy_no_ac:
        _escalate(
            db,
            task,
            "todo task has no acceptance_criteria; refusing to dispatch (fail-closed)",
        )
        return "escalated_missing_ac"

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
    try:
        result = service.request_review(
            task_id=task.id,
            reviewer=reviewer,
            actor="system:orchestration-driver",
            idempotency_key=f"advance:{task.id}:review:r{round_}",
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
