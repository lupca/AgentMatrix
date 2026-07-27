"""Dramatiq actor that executes CLI agents with durable state and streaming."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone

import dramatiq
import psutil
from dramatiq.middleware import CurrentMessage
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import AgentOutputChunk, AgentRun, AuditLog, Task, TaskDependency
from app.services.agent_matcher import AgentMatcher
from app.services.command_builder import _is_review_task, review_result_path
from app.schemas.task import ReviewResult
from app.services.process_manager import (
    ProcessManager,
    ProcessResult,
    ProcessStatus,
    WorktreeManager,
    WorktreeUnsupportedError,
)
from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService
from app.workers import redis_broker
from app.workers.output_streamer import (
    clear_cancel_request,
    get_channel,
    is_cancel_requested,
    redis_client,
)

logger = logging.getLogger(__name__)

OUTPUT_CHUNK_LINES = max(1, int(os.getenv("AGENT_OUTPUT_CHUNK_LINES", "100")))

# Cap on changes-requested -> todo replan rounds, and on consecutive
# advance_task calls that see the same actionable status with no forward
# movement. Read from a constant for now; CTV2-093 moves this to policy.
AUTO_MAX_ROUNDS = max(1, int(os.getenv("AUTO_MAX_ROUNDS", "3")))

# Each AgentRun executes in its own `git worktree` (CTV2-105) so concurrent
# runs against the same repo never contend on `.git/index.lock` or see each
# other's uncommitted state. Set to "0"/"false" to fall back to running
# every agent directly in the shared repo root (fully sequential, as before
# CTV2-105) -- e.g. for a repo/environment where worktrees are unavailable.
WORKTREE_ENABLED = os.getenv("AGENT_RUN_USE_WORKTREE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


def _use_worktree() -> bool:
    return WORKTREE_ENABLED


class AgentExecutionError(RuntimeError):
    """A retryable agent process failure."""


class ReviewResultLoadError(ValueError):
    """Structured failure while loading a code-review result artifact."""

    def __init__(self, code: str, path: str, message: str, **details):
        self.code = code
        self.path = path
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"code": self.code, "path": self.path, "details": self.details}


def load_review_result(
    repo_root: str,
    task_id: str,
    acceptance_criteria: list | None = None,
) -> ReviewResult:
    """Read and strictly validate the review artifact written by the agent."""
    path = review_result_path(repo_root, task_id)
    try:
        with open(path, encoding="utf-8") as result_file:
            raw = result_file.read()
    except FileNotFoundError as exc:
        raise ReviewResultLoadError(
            "missing_file", path, "Review result file is missing"
        ) from exc
    except OSError as exc:
        raise ReviewResultLoadError(
            "read_error", path, "Review result file could not be read", error=str(exc)
        ) from exc

    if not raw.strip():
        raise ReviewResultLoadError("empty_file", path, "Review result file is empty")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReviewResultLoadError(
            "invalid_json", path, "Review result is not valid JSON", line=exc.lineno,
            column=exc.colno,
        ) from exc

    try:
        result = ReviewResult.model_validate(payload)
    except ValidationError as exc:
        error_types = {error.get("type") for error in exc.errors()}
        code = "missing_required_field" if "missing" in error_types else (
            "invalid_type"
            if any(
                isinstance(error_type, str) and "type" in error_type
                for error_type in error_types
            )
            else "schema_validation"
        )
        raise ReviewResultLoadError(
            code, path, "Review result does not match its schema",
            errors=exc.errors(),
        ) from exc

    if result.task_id != task_id:
        raise ReviewResultLoadError(
            "task_id_mismatch", path, "Review result task_id does not match the run",
            expected=task_id, actual=result.task_id,
        )
    expected_count = len(acceptance_criteria or [])
    if len(result.ac_results) != expected_count:
        raise ReviewResultLoadError(
            "acceptance_criteria_count_mismatch",
            path,
            "Review result must contain one result per acceptance criterion",
            expected=expected_count,
            actual=len(result.ac_results),
        )
    return result


def publish_line(
    run_id: str,
    line: str,
    line_type: str = "stdout",
    *,
    line_index: int | None = None,
) -> None:
    """Publish a line; kept here as a stable public worker API."""
    payload = {
        "type": line_type,
        "content": line,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if line_index is not None:
        payload["index"] = line_index
    _publish(run_id, payload)


def publish_status(run_id: str, status: str, **kwargs) -> None:
    """Publish a lifecycle event; kept here for callers and unit tests."""
    _publish(
        run_id,
        {
            "type": "status",
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        },
    )


def _nudge_driver(task_id: str, trigger: str) -> None:
    """Best-effort event-driven wake-up of the orchestration driver.

    Never allowed to fail the caller: a dropped nudge just leaves the task
    waiting for the next trigger (or a manual command) instead of crashing an
    otherwise-successful run/gate transition.
    """
    try:
        advance_task.send(task_id, trigger)
    except Exception:
        logger.warning(
            "Could not enqueue advance_task for task %s (trigger=%s)",
            task_id,
            trigger,
            exc_info=True,
        )


def _publish(run_id: str, payload: dict) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    for attempt in range(3):
        try:
            redis_client.publish(get_channel(run_id), encoded)
            return
        except Exception:
            if attempt == 2:
                logger.warning("Unable to publish event for agent run %s", run_id)
                return
            time.sleep(0.1 * (2**attempt))


@dramatiq.actor(
    broker=redis_broker,
    max_retries=3,
    min_backoff=30_000,
    max_backoff=300_000,
    time_limit=900_000,
    notify_shutdown=True,
)
def run_agent(
    run_id: str,
    task_id: str,
    command: str,
    repo_root: str,
    timeout_seconds: int = 900,
) -> int | None:
    """Execute an agent and persist/stream its full lifecycle."""
    db: Session = SessionLocal()
    cancel_check = _throttled_cancel_check(run_id)
    process_manager = ProcessManager(
        timeout_seconds=timeout_seconds,
        cancel_check=cancel_check,
    )
    worktree_manager: WorktreeManager | None = None
    worktree_path: str | None = None
    exec_cwd = repo_root

    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run is None:
            logger.error("AgentRun %s does not exist; discarding message", run_id)
            return None

        # Duplicate delivery after a completed attempt is safe and does no work.
        # This must run before the brake check below: a terminal run is no
        # longer a spawn candidate, and brake-tripping it here would
        # overwrite an already-successful run's status with "cancelled".
        if run.status in {"success", "timeout", "cancelled"} or (
            run.status == "failed" and run.attempt >= run.max_attempts
        ):
            logger.info("Ignoring duplicate delivery for terminal run %s", run_id)
            return run.exit_code

        # The service normally performs this check before enqueueing.  Repeat
        # it immediately before creating a process because queued messages can
        # outlive a setting change or race another worker.  A concurrency trip
        # is intentionally retryable: the run remains queued and no process
        # is spawned.
        brake = TaskOrchestrationService(db).check_brakes(
            run.task, for_spawn=True, audit=True, run_id=run.id
        )
        if not brake.allowed:
            if brake.queue:
                run.status = "queued"
                run.error_message = brake.reason
                db.commit()
                raise AgentExecutionError(brake.reason)
            run.status = "cancelled"
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = brake.reason
            db.commit()
            return None

        _cleanup_stale_process(run)
        task = run.task
        # `run.kind == "review"` (CTV2-086) is the authoritative signal for a
        # review run against the task it targets. The title/raw_input text
        # heuristic is kept only for the older "review is its own Task" flow.
        is_review_run = run.kind == "review"
        is_review_task = task is not None and (
            is_review_run or _is_review_task(task)
        )
        if is_review_task:
            _prepare_review_artifact(repo_root, task_id)
        attempt = _current_attempt(run)
        run.status = "running"
        run.attempt = attempt
        run.started_at = datetime.now(timezone.utc)
        run.completed_at = None
        run.exit_code = None
        run.pid = None
        run.error_message = None
        # Keep the baseline on the durable run record.  This is intentionally
        # written before the process starts so a retry cannot silently move
        # the review boundary forward.
        base_ref = _run_base_ref(run.result_ref) or _parse_result_ref(repo_root)
        if base_ref is None:
            run.status = "failed"
            run.error_message = "Could not determine repository HEAD before execution"
            db.commit()
            TaskOrchestrationService(db).record_execution_failure(
                task_id=task_id,
                error=run.error_message,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:missing-base",
                run_id=run.id,
            )
            _nudge_driver(task_id, "run_agent_completed")
            return None

        if _use_worktree():
            worktree_manager = WorktreeManager(repo_root)
            try:
                worktree_path = worktree_manager.create(run.id, base_ref)
                exec_cwd = worktree_path
            except WorktreeUnsupportedError as exc:
                logger.warning(
                    "git worktree unavailable for %s (%s); run %s falls back to "
                    "the shared sequential working tree",
                    repo_root,
                    exc,
                    run_id,
                )
                worktree_manager = None
                worktree_path = None
                exec_cwd = repo_root

        if _has_uncommitted_changes(exec_cwd):
            logger.warning(
                "Repository %s was dirty before agent execution; "
                "only the committed base..head range will be reviewed",
                exec_cwd,
            )
        run.result_ref = f"{base_ref}.."
        db.commit()

        def record_pid(pid: int) -> None:
            run.pid = pid
            db.commit()

        process_manager.on_start = record_pid
        publish_status(run_id, "running", attempt=attempt)
        logger.info(
            "Starting agent run %s for task %s (attempt %d/%d)",
            run_id,
            task_id,
            attempt,
            run.max_attempts,
        )

        starting_lines = run.output_lines or 0
        starting_bytes = run.output_bytes or 0
        line_count = starting_lines
        total_bytes = starting_bytes
        chunk_buffer: list[str] = []
        explicit_result_ref: str | None = None
        active_chunk: AgentOutputChunk | None = None
        next_chunk_index = _next_chunk_index(db, run_id)
        result: ProcessResult | None = None

        for output in process_manager.run_with_streaming(command, exec_cwd):
            if isinstance(output, ProcessResult):
                result = output
                break

            line_count += 1
            total_bytes += len(output.encode("utf-8"))
            chunk_buffer.append(output)
            explicit_result_ref = (
                _extract_explicit_result_ref(output) or explicit_result_ref
            )

            if active_chunk is None:
                active_chunk = AgentOutputChunk(
                    run_id=run_id,
                    chunk_index=next_chunk_index,
                    content="",
                )
                db.add(active_chunk)
            active_chunk.content = "\n".join(chunk_buffer)
            run.output_lines = line_count
            run.output_bytes = total_bytes
            db.commit()

            # Publish only after persistence so a reconnect can always replay
            # every line that it might have missed from Pub/Sub.
            publish_line(run_id, output, line_index=line_count)

            if len(chunk_buffer) >= OUTPUT_CHUNK_LINES:
                chunk_buffer.clear()
                active_chunk = None
                next_chunk_index += 1

        if result is None:
            result = ProcessResult(
                ProcessStatus.FAILED,
                -1,
                "Agent process ended without a result",
            )

        run.output_lines = line_count
        run.output_bytes = total_bytes
        run.exit_code = result.exit_code
        run.pid = None
        run.error_message = result.error

        if result.status == ProcessStatus.FAILED and attempt < run.max_attempts:
            run.status = "queued"
            run.completed_at = None
            db.commit()
            publish_status(
                run_id,
                "retrying",
                attempt=attempt,
                max_attempts=run.max_attempts,
                exit_code=result.exit_code,
                error=result.error,
            )
            raise AgentExecutionError(result.error or "Agent process failed")

        run.status = result.status.value
        run.completed_at = datetime.now(timezone.utc)
        effective_status = result.status.value
        if result.status == ProcessStatus.COMPLETED:
            if is_review_run and task is not None:
                try:
                    review_result = load_review_result(
                        repo_root,
                        task_id,
                        task.acceptance_criteria or [],
                    )
                except ReviewResultLoadError as exc:
                    # Missing/malformed artifact is never treated as an
                    # implicit pass. The task is escalated to a human rather
                    # than silently advanced or left stuck in "in-review".
                    run.status = ProcessStatus.FAILED.value
                    effective_status = ProcessStatus.FAILED.value
                    run.error_message = str(exc)
                    TaskOrchestrationService(db).record_review_failure(
                        task_id=task_id,
                        error=str(exc),
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:review-result-invalid",
                        run_id=run.id,
                    )
                else:
                    # The JSON artifact is the durable review result. Keep the
                    # ref relative to repo_root so it stays portable.
                    run.result_ref = os.path.relpath(
                        review_result_path(repo_root, review_result.task_id),
                        repo_root,
                    )
                    _submit_review_verdict(db, run, review_result)
            elif is_review_task and task is not None:
                review_result = load_review_result(
                    repo_root,
                    task_id,
                    task.acceptance_criteria or [],
                )
                # The JSON artifact is the durable review result. Keep the ref
                # relative to repo_root so it remains portable to CTV2-099.
                run.result_ref = os.path.relpath(
                    review_result_path(repo_root, review_result.task_id), repo_root
                )
            else:
                result_ref, no_change_error = _build_execution_result_ref(
                    exec_cwd,
                    base_ref,
                    explicit_result_ref,
                )
                if no_change_error:
                    run.status = ProcessStatus.FAILED.value
                    effective_status = ProcessStatus.FAILED.value
                    run.error_message = no_change_error
                    TaskOrchestrationService(db).record_execution_failure(
                        task_id=task_id,
                        error=no_change_error,
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:no-committed-changes",
                        run_id=run.id,
                    )
                else:
                    run.result_ref = result_ref
                    TaskOrchestrationService(db).record_execution_success(
                        task_id=task_id,
                        result_ref=run.result_ref,
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:execution-success",
                        run_id=run.id,
                    )
        elif is_review_run:
            TaskOrchestrationService(db).record_review_failure(
                task_id=task_id,
                error=result.error or result.status.value,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:review-{result.status.value}",
                run_id=run.id,
            )
        else:
            TaskOrchestrationService(db).record_execution_failure(
                task_id=task_id,
                error=result.error or result.status.value,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:execution-{result.status.value}",
                run_id=run.id,
            )
        db.commit()
        _nudge_driver(task_id, "run_agent_completed")

        effective_error = run.error_message or result.error
        publish_status(
            run_id,
            effective_status,
            attempt=attempt,
            exit_code=result.exit_code,
            result_ref=run.result_ref,
            error=effective_error,
        )
        clear_cancel_request(run_id)
        logger.info("Agent run %s completed: %s", run_id, effective_status)
        return result.exit_code
    except AgentExecutionError:
        # State is already set to queued/retrying. Raising lets Dramatiq apply
        # its durable retry/backoff policy.
        raise
    except Exception as exc:
        logger.exception("Agent run %s failed unexpectedly", run_id)
        db.rollback()
        should_retry = _record_unexpected_failure(db, run_id, task_id, exc)
        if should_retry:
            publish_status(run_id, "retrying", error=str(exc))
            raise
        publish_status(run_id, "failed", error=str(exc))
        _nudge_driver(task_id, "run_agent_completed")
        return None
    finally:
        process_manager.terminate()
        if worktree_manager is not None and worktree_path is not None:
            worktree_manager.remove(worktree_path)
        db.close()


# Statuses where the driver takes a mechanical action rather than waiting.
_ACTIONABLE_STATUSES = {"todo", "awaiting-review", "changes-requested"}


@dramatiq.actor(
    broker=redis_broker,
    max_retries=0,
    time_limit=60_000,
)
def advance_task(task_id: str, trigger: str) -> str:
    """Event-driven, 0-token orchestration driver.

    Reads (status, mode, awaiting_approval), applies the §3.1 decision table,
    and calls TaskOrchestrationService for the next mechanical transition. It
    is invoked after run_agent finishes, after a gate is approved, after a
    review run completes, and after a `changes` verdict is recorded -- never
    polled. It never sets task fields itself.
    """
    db: Session = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).with_for_update().first()
        if task is None:
            logger.warning("advance_task: task %s not found", task_id)
            return "not_found"

        status_before = task.status
        service = TaskOrchestrationService(db)
        if task.awaiting_approval:
            # Already parked for a human decision (a gate pending, or a
            # brake escalation) -- that is the correct stopped state, not a
            # driver loop, so it never counts toward the stall cap.
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
            # `_escalate` (missing AC, no agent, a failed dependency, a
            # round/stall cap, ...) sets task.status directly rather than
            # going through TaskOrchestrationService, so this is the one
            # place that catches every escalation path and wakes whatever
            # is waiting on this task (CTV2-094).
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
    """Detect AUTO_MAX_ROUNDS consecutive calls stuck at the same actionable status.

    Only actionable statuses can stall this way -- `dispatched`/`in-review`
    are expected to sit unchanged between events, and `done`/`failed` are
    terminal, so neither is a loop.
    """
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
    task.status = "failed"
    task.error = reason
    task.awaiting_approval = True
    task.approval_prompt = reason
    task.updated_at = datetime.now(timezone.utc)


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
        return f"brake:{brake.code}"

    return _dispatch_execute(db, service, task)


def _blocked_by_dependencies(db: Session, task: Task) -> str | None:
    """Gate dispatch on every task_dependencies edge reaching `done` (CTV2-094).

    A missing or `failed` dependency can never reach `done`, so the
    dependent task is escalated immediately instead of waiting forever;
    an unmet-but-still-active dependency just parks the task -- it is woken
    by `TaskOrchestrationService.wake_dependents` once the dependency closes.
    """
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

    executor_norm = (task.executor or "").strip().casefold()
    suggestions = AgentMatcher(db).suggest_agents(task, top_n=5)
    independent = [
        s for s in suggestions if s.agent_id.strip().casefold() != executor_norm
    ]
    if not independent:
        _escalate(db, task, f"no independent reviewer available for task {task.id}")
        return "escalated_no_reviewer"
    reviewer = independent[0].agent_id

    round_ = service.changes_round_count(task.id)
    try:
        result = service.request_review(
            task_id=task.id,
            reviewer=reviewer,
            actor="system:orchestration-driver",
            idempotency_key=f"advance:{task.id}:review:r{round_}",
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
    if round_ >= AUTO_MAX_ROUNDS:
        _escalate(
            db,
            task,
            f"changes-requested round limit reached ({round_}/{AUTO_MAX_ROUNDS}); "
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


def _current_attempt(run: AgentRun) -> int:
    message = CurrentMessage.get_current_message()
    broker_attempt = 1
    if message is not None:
        broker_attempt = int(message.options.get("retries", 0)) + 1

    persisted_attempt = int(run.attempt or 1)
    if run.started_at is not None and run.status in {"queued", "running", "failed"}:
        persisted_attempt += 1
    return max(broker_attempt, persisted_attempt)


def _throttled_cancel_check(run_id: str, interval: float = 0.5):
    last_check = 0.0
    last_result = False

    def check() -> bool:
        nonlocal last_check, last_result
        now = time.monotonic()
        if last_result or now - last_check >= interval:
            last_check = now
            last_result = is_cancel_requested(run_id)
        return last_result

    return check


def _cleanup_stale_process(run: AgentRun, grace_seconds: float = 2.0) -> None:
    """Clean an orphan left by a hard worker crash before message redelivery."""
    if run.status != "running" or not run.pid:
        return

    try:
        parent = psutil.Process(run.pid)
        command_line = " ".join(parent.cmdline())
        if run.command not in command_line:
            logger.warning(
                "Refusing to kill stale PID %s for run %s: command does not match",
                run.pid,
                run.id,
            )
            return
        if os.getpgid(run.pid) != run.pid:
            logger.warning(
                "Refusing to kill stale PID %s for run %s: not a process-group leader",
                run.pid,
                run.id,
            )
            return
        processes = parent.children(recursive=True) + [parent]
        os.killpg(run.pid, signal.SIGTERM)
        _, alive = psutil.wait_procs(processes, timeout=grace_seconds)
        for process in alive:
            process.kill()
        if alive:
            psutil.wait_procs(alive, timeout=grace_seconds)
        logger.info("Cleaned stale process tree %s for run %s", run.pid, run.id)
    except (psutil.NoSuchProcess, ProcessLookupError):
        return
    except (psutil.Error, OSError):
        logger.exception("Could not clean stale process %s for run %s", run.pid, run.id)


def _next_chunk_index(db: Session, run_id: str) -> int:
    highest = (
        db.query(func.max(AgentOutputChunk.chunk_index))
        .filter(AgentOutputChunk.run_id == run_id)
        .scalar()
    )
    return 0 if highest is None else highest + 1


def _parse_result_ref(repo_root: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip()[:12] if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


_EXPLICIT_RESULT_REF = re.compile(
    r"^\s*(?:result[_-]ref|result reference)\s*:\s*([^\s]+)\s*$",
    re.IGNORECASE,
)


def _extract_explicit_result_ref(line: str) -> str | None:
    """Read the optional commit ref convention emitted by an executor."""
    match = _EXPLICIT_RESULT_REF.match(line)
    return match.group(1) if match else None


def _run_base_ref(result_ref: str | None) -> str | None:
    """Recover a baseline persisted as ``base..`` during an active run."""
    if not result_ref or ".." not in result_ref:
        return None
    base, _ = result_ref.split("..", 1)
    return base or None


def _git_ref(repo_root: str, ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _is_ancestor(repo_root: str, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_root,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _has_committed_diff(repo_root: str, base: str, head: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", base, head],
            cwd=repo_root,
            timeout=10,
            check=False,
        )
        return result.returncode == 1
    except (OSError, subprocess.SubprocessError):
        return False


def _has_uncommitted_changes(repo_root: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _build_execution_result_ref(
    repo_root: str,
    base_ref: str,
    explicit_ref: str | None = None,
) -> tuple[str | None, str | None]:
    """Return a validated review range, or a concrete no-change error."""
    head_ref = _parse_result_ref(repo_root)
    if head_ref is None:
        return None, "Could not determine repository HEAD after execution"
    base = _git_ref(repo_root, base_ref)
    head = _git_ref(repo_root, head_ref)
    if base is None or head is None:
        return None, "Could not validate repository execution range"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if status.returncode == 0 and status.stdout.strip():
        logger.warning(
            "Repository %s has uncommitted changes after agent execution; "
            "only committed changes will be reviewed",
            repo_root,
        )

    if base == head or not _has_committed_diff(repo_root, base, head):
        return None, "Agent completed without committed changes; escalating for review"

    selected = head
    if explicit_ref:
        explicit = _git_ref(repo_root, explicit_ref)
        if explicit is None or not _is_ancestor(repo_root, base, explicit) or not _is_ancestor(repo_root, explicit, head):
            return None, "Executor result-ref is outside the actual base..head range"
        selected = explicit
    if not _has_committed_diff(repo_root, base, selected):
        return None, "Executor result-ref points to an empty diff"
    return f"{base[:12]}..{selected[:12]}", None


def _submit_review_verdict(db: Session, run: AgentRun, review_result: ReviewResult) -> None:
    """Auto-submit the verdict derived strictly from the validated review JSON.

    This is the only place a review's pass/changes verdict is decided — it
    reads the structured ``ReviewResult`` artifact, never free-form CLI
    output. The actor is the review run's own agent, not a value a
    coordinator/LLM tool call could spoof.
    """
    ac_results = [
        {
            "ac_index": ac.ac_index,
            "ac_text": ac.ac_text,
            "passed": ac.verdict == "pass",
            "evidence": ac.evidence,
        }
        for ac in review_result.ac_results
    ]
    verdict = "pass" if all(item["passed"] for item in ac_results) else "changes"
    try:
        TaskOrchestrationService(db).request_verdict(
            task_id=run.task_id,
            verdict=verdict,
            ac_results=ac_results,
            findings=review_result.findings,
            actor=f"agent:{run.agent_id}",
            idempotency_key=f"run:{run.id}:review-verdict",
        )
    except OrchestrationError as exc:
        TaskOrchestrationService(db).record_review_failure(
            task_id=run.task_id,
            error=f"Could not record verdict from review result: {exc}",
            actor=f"agent:{run.agent_id}",
            idempotency_key=f"run:{run.id}:review-verdict-failed",
            run_id=run.id,
        )


def _prepare_review_artifact(repo_root: str, task_id: str) -> None:
    """Create the ignored artifact directory and remove stale task output."""
    path = review_result_path(repo_root, task_id)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    ignore_path = os.path.join(directory, ".gitignore")
    if not os.path.exists(ignore_path):
        with open(ignore_path, "w", encoding="utf-8") as ignore_file:
            ignore_file.write("review-*.json\n")
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _update_task_status(
    db: Session,
    task_id: str,
    status: str,
    *,
    result_ref: str | None = None,
    error: str | None = None,
) -> None:
    """Compatibility wrapper; lifecycle writes remain service-owned."""
    service = TaskOrchestrationService(db)
    if status in {"done", "awaiting-review"}:
        service.record_execution_success(
            task_id=task_id,
            result_ref=result_ref,
            actor="system:agent-worker",
            idempotency_key=f"legacy-worker:{task_id}:success:{result_ref}",
        )
    else:
        service.record_execution_failure(
            task_id=task_id,
            error=error or status,
            actor="system:agent-worker",
            idempotency_key=f"legacy-worker:{task_id}:failure:{status}",
        )


def _record_unexpected_failure(
    db: Session,
    run_id: str,
    task_id: str,
    exc: Exception,
) -> bool:
    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        should_retry = run is None or run.attempt < run.max_attempts
        if run is not None and should_retry:
            run.status = "queued"
            run.pid = None
            run.error_message = str(exc)
            run.completed_at = None
        elif run is not None:
            run.status = "failed"
            run.pid = None
            run.error_message = str(exc)
            run.completed_at = datetime.now(timezone.utc)
            TaskOrchestrationService(db).record_execution_failure(
                task_id=task_id,
                error=str(exc),
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:unexpected-failure",
                run_id=run.id,
            )
        db.commit()
        return should_retry
    except Exception:
        db.rollback()
        logger.exception("Could not persist unexpected failure for run %s", run_id)
        return True
