"""Dramatiq actor that executes CLI agents with durable state and streaming."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone

import dramatiq
import psutil
from dramatiq.middleware import CurrentMessage
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import AgentOutputChunk, AgentRun, Task
from app.services.process_manager import ProcessManager, ProcessResult, ProcessStatus
from app.workers import redis_broker
from app.workers.output_streamer import (
    clear_cancel_request,
    get_channel,
    is_cancel_requested,
    redis_client,
)

logger = logging.getLogger(__name__)

OUTPUT_CHUNK_LINES = max(1, int(os.getenv("AGENT_OUTPUT_CHUNK_LINES", "100")))


class AgentExecutionError(RuntimeError):
    """A retryable agent process failure."""


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
    time_limit=14_400_000,
    notify_shutdown=True,
)
def run_agent(
    run_id: str,
    task_id: str,
    command: str,
    repo_root: str,
    timeout_seconds: int = 14_400,
) -> int | None:
    """Execute an agent and persist/stream its full lifecycle."""
    db: Session = SessionLocal()
    cancel_check = _throttled_cancel_check(run_id)
    process_manager = ProcessManager(
        timeout_seconds=timeout_seconds,
        cancel_check=cancel_check,
    )

    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run is None:
            logger.error("AgentRun %s does not exist; discarding message", run_id)
            return None

        # Duplicate delivery after a completed attempt is safe and does no work.
        if run.status in {"success", "timeout", "cancelled"} or (
            run.status == "failed" and run.attempt >= run.max_attempts
        ):
            logger.info("Ignoring duplicate delivery for terminal run %s", run_id)
            return run.exit_code

        _cleanup_stale_process(run)
        attempt = _current_attempt(run)
        run.status = "running"
        run.attempt = attempt
        run.started_at = datetime.now(timezone.utc)
        run.completed_at = None
        run.exit_code = None
        run.pid = None
        run.error_message = None
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
        active_chunk: AgentOutputChunk | None = None
        next_chunk_index = _next_chunk_index(db, run_id)
        result: ProcessResult | None = None

        for output in process_manager.run_with_streaming(command, repo_root):
            if isinstance(output, ProcessResult):
                result = output
                break

            line_count += 1
            total_bytes += len(output.encode("utf-8"))
            chunk_buffer.append(output)

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
        if result.status == ProcessStatus.COMPLETED:
            run.result_ref = _parse_result_ref(repo_root)
            _update_task_status(db, task_id, "done", result_ref=run.result_ref)
        else:
            _update_task_status(db, task_id, "failed", error=result.error)
        db.commit()

        publish_status(
            run_id,
            result.status.value,
            attempt=attempt,
            exit_code=result.exit_code,
            result_ref=run.result_ref,
            error=result.error,
        )
        clear_cancel_request(run_id)
        logger.info("Agent run %s completed: %s", run_id, result.status.value)
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
        return None
    finally:
        process_manager.terminate()
        db.close()


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


def _update_task_status(
    db: Session,
    task_id: str,
    status: str,
    *,
    result_ref: str | None = None,
    error: str | None = None,
) -> None:
    task = db.query(Task).filter(Task.id == task_id).first()
    if task is None:
        return

    now = datetime.now(timezone.utc)
    task.status = status
    task.updated_at = now
    if result_ref:
        task.result_ref = result_ref
    if error:
        task.error = error
    if status == "done":
        task.completed_at = now


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
            _update_task_status(db, task_id, "failed", error=str(exc))
        db.commit()
        return should_retry
    except Exception:
        db.rollback()
        logger.exception("Could not persist unexpected failure for run %s", run_id)
        return True
