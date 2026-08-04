"""CLI agent execution and process management utilities."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import psutil
from dramatiq.middleware import CurrentMessage
from sqlalchemy.orm import Session

from app.db.base import SessionLocal
from app.db.models import (
    AgentEvent,
    AgentOutputChunk,
    Agent,
    AgentRun,
    LLMUsage,
    RunResourceUsage,
    VendorRawEvent,
)
from app.schemas.task import ReviewResult
from app.services.command_builder import _is_review_task, review_result_path
from app.services.coordinator import CoordinatorService
from app.services.process_manager import (
    ProcessManager,
    ProcessResult,
    ProcessStatus,
    WorktreeManager,
    WorktreeUnsupportedError,
)
from app.services.outbox import record_commit_event
from app.services.task_event_service import emit_task_event
from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService
from app.services.tool_metrics import record_tool_metric
from app.workers.output_parser import (
    OUTPUT_CHUNK_LINES,
    ReviewResultLoadError,
    _extract_explicit_result_ref,
    _next_agent_event_seq,
    _next_chunk_index,
    _record_agent_event,
    _record_vendor_output,
    load_review_result,
    parse_cli_token_usage,
    parse_vendor_event,
)
from app.workers.output_streamer import (
    clear_cancel_request,
    get_channel,
    is_cancel_requested,
    redis_client,
)

logger = logging.getLogger("app.workers.agent_runner")

WORKTREE_ENABLED = os.getenv("AGENT_RUN_USE_WORKTREE", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}


def _runner():
    """Get agent_runner module dynamically so monkeypatched attributes are respected."""
    return sys.modules.get("app.workers.agent_runner")


def _get_attr(name: str, fallback: Any):
    mod = _runner()
    return getattr(mod, name, fallback) if mod is not None else fallback


def _use_worktree() -> bool:
    return bool(_get_attr("WORKTREE_ENABLED", WORKTREE_ENABLED))


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
    client = _get_attr("redis_client", redis_client)
    for attempt in range(3):
        try:
            client.publish(get_channel(run_id), encoded)
            return
        except Exception:
            if attempt == 2:
                logger.warning("Unable to publish event for agent run %s", run_id)
                return
            time.sleep(0.1 * (2**attempt))


def _nudge_driver(task_id: str, trigger: str) -> None:
    try:
        mod = _runner()
        if mod and hasattr(mod, "advance_task"):
            mod.advance_task.send(task_id, trigger)
        else:
            from app.workers.agent_runner import advance_task
            advance_task.send(task_id, trigger)
    except Exception:
        logger.warning(
            "Could not enqueue advance_task for task %s (trigger=%s)",
            task_id,
            trigger,
            exc_info=True,
        )


def _enqueue_coordinator_wake(event_id: int) -> None:
    try:
        mod = _runner()
        if mod and hasattr(mod, "wake_coordinator"):
            mod.wake_coordinator.send(event_id)
        else:
            from app.workers.agent_runner import wake_coordinator
            wake_coordinator.send(event_id)
    except Exception:
        logger.warning(
            "Could not enqueue coordinator wake for task event %s",
            event_id,
            exc_info=True,
        )


def _emit_decision_event(
    db: Session,
    *,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
):
    fn = _get_attr("emit_task_event", emit_task_event)
    event = fn(
        task_id=task_id,
        event_type=event_type,
        kind="decision",
        payload=payload,
        db=db,
    )
    _enqueue_coordinator_wake(event.id)
    return event


def _decision_status_message(event) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    result = payload.get("result")
    if result is None:
        result = payload.get("result_ref")
    if result is None and "exit_code" in payload:
        result = {"exit_code": payload["exit_code"]}
    status = {
        "type": "task_decision",
        "source_event_id": event.id,
        "task_id": event.task_id,
        "step": event.event_type,
        "result": result,
        "error": payload.get("error") or payload.get("reason"),
        "available_actions": {
            "dispatch": {
                "tool": "dispatch_task",
                "task_id": event.task_id,
                "purpose": "retry or dispatch the failed task",
            },
            "cancel": {"tool": "cancel_task", "task_id": event.task_id},
            "update_task": {"tool": "update_task", "task_id": event.task_id},
            "verdict": {"tool": "record_verdict", "task_id": event.task_id},
        },
        "event_payload": payload,
    }
    return "TASK_DECISION_EVENT\n" + json.dumps(
        status,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _cleanup_mcp_config(paths: list[str] | None) -> None:
    from app.services.mcp_attach import detach_mcp

    detach_mcp(paths)


def _record_run_resource_usage(db: Session, run: AgentRun) -> None:
    events = db.query(AgentEvent).filter(AgentEvent.run_id == run.id).all()
    usages = db.query(LLMUsage).filter(LLMUsage.agent_run_id == run.id).all()
    tool_events = [event for event in events if event.event_type == "tool.started"]
    bash_commands = 0
    files_read = files_written = rate_limit_events = 0
    for event in events:
        payload = event.payload or {}
        payload_text = json.dumps(payload).lower()
        if event.event_type == "tool.started" and any(
            term in payload_text for term in ("bash", "shell", "terminal", "command")
        ):
            bash_commands += 1
        if event.event_type == "workspace.changed":
            files_read += int(payload.get("files_read", 0) or 0)
            files_written += int(payload.get("files_written", 0) or 0)
        if any(term in payload_text for term in ("rate limit", "rate_limit", "ratelimit", "429")):
            rate_limit_events += 1
    raw_events = db.query(VendorRawEvent).filter(VendorRawEvent.run_id == run.id).all()
    rate_limit_events += sum(
        bool(re.search(r"rate[ -]?limit|429|quota exceeded", event.raw_output, re.I))
        for event in raw_events
    )
    active_seconds = 0.0
    if run.started_at and run.completed_at:
        active_seconds = max(0.0, (run.completed_at - run.started_at).total_seconds())
    usage = db.get(RunResourceUsage, run.id)
    if usage is None:
        usage = RunResourceUsage(agent_run_id=run.id)
        db.add(usage)
    usage.llm_calls = len(usages)
    usage.input_tokens = sum(item.input_tokens or 0 for item in usages)
    usage.output_tokens = sum(item.output_tokens or 0 for item in usages)
    usage.tool_calls = len(tool_events)
    usage.bash_commands = bash_commands
    usage.files_read = files_read
    usage.files_written = files_written
    usage.active_seconds = active_seconds
    usage.rate_limit_events = rate_limit_events
    # CLI subscription costUSD is a vendor-reported estimate, not an
    # authoritative bill.  Keep it in LLMUsage for observability, but never
    # expose it as the run's authoritative estimated cost.
    usage.estimated_cost_usd = sum(
        (item.cost_usd or 0) for item in usages if item.operation != "cli"
    )


def _record_cli_usage(db: Session, run: AgentRun, cli: str, stdout: str) -> None:
    """Parse a completed CLI result and append its usage ledger row.

    The raw output is already persisted as ``VendorRawEvent`` rows while the
    process streams, so callers can pass it here without retaining a second
    copy of potentially large agent output in memory.  A row is attributed to
    both the run and task because either scope is used by cost reporting.
    """
    usage_data = parse_cli_token_usage(cli, stdout)
    if not usage_data:
        return

    normalized_cli = (cli or "").strip().lower()
    if normalized_cli not in {"claude", "qwen", "agy", "codex"}:
        return
    existing = (
        db.query(LLMUsage)
        .filter(
            LLMUsage.agent_run_id == run.id,
            LLMUsage.provider == normalized_cli,
            LLMUsage.operation == "cli",
        )
        .first()
    )
    if existing is not None:
        return

    agent = db.get(Agent, run.agent_id)
    db.add(
        LLMUsage(
            agent_run_id=run.id,
            task_id=run.task_id,
            model=(agent.model if agent and agent.model else normalized_cli),
            provider=normalized_cli,
            operation="cli",
            input_tokens=usage_data["input_tokens"],
            output_tokens=usage_data["output_tokens"],
            cached_tokens=usage_data["cached_tokens"],
            # Qwen/Agy expose token counts but no authoritative USD amount;
            # keep the ledger schema's numeric default and surface that fact
            # through get_stats rather than presenting zero as free.
            cost_usd=usage_data.get("cost_usd", 0),
        )
    )
    db.flush()


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
    check_fn = _get_attr("is_cancel_requested", is_cancel_requested)

    def check() -> bool:
        nonlocal last_check, last_result
        now = time.monotonic()
        if last_result or now - last_check >= interval:
            last_check = now
            last_result = check_fn(run_id)
        return last_result

    return check


def _cleanup_stale_process(run: AgentRun, grace_seconds: float = 2.0) -> None:
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


def _run_base_ref(result_ref: str | None) -> str | None:
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
    base = _git_ref(repo_root, base_ref)
    if base is None:
        return None, "Could not validate base ref for execution range"

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

    if explicit_ref:
        explicit = _git_ref(repo_root, explicit_ref)
        if explicit is not None:
            if not _is_ancestor(repo_root, base, explicit):
                return None, "Executor result-ref is not a descendant of base"
            if _has_committed_diff(repo_root, base, explicit):
                return f"{base[:12]}..{explicit[:12]}", None
            return None, "Executor result-ref points to an empty diff"
        return None, f"Executor result-ref {explicit_ref} is outside the actual base..head range"

    head_ref = _parse_result_ref(repo_root)
    if head_ref is None:
        return None, "Could not determine repository HEAD after execution"
    head = _git_ref(repo_root, head_ref)
    if head is None:
        return None, "Could not validate repository HEAD"

    if base == head or not _has_committed_diff(repo_root, base, head):
        return None, "Agent completed without committed changes; escalating for review"

    return f"{base[:12]}..{head[:12]}", None


def _submit_review_verdict(db: Session, run: AgentRun, review_result: ReviewResult) -> None:
    ac_results = [
        {
            "ac_index": ac.ac_index,
            "ac_text": ac.ac_text,
            "passed": ac.verdict == "pass",
            "evidence": ac.evidence,
            "criterion_id": ac.criterion_id,
            "status": ac.status,
            "finding_ids": ac.finding_ids,
        }
        for ac in review_result.ac_results
    ]
    verdict = "pass" if all(item["passed"] for item in ac_results) else "changes"
    orch_cls = _get_attr("TaskOrchestrationService", TaskOrchestrationService)
    try:
        orch_cls(db).request_verdict(
            task_id=run.task_id,
            verdict=verdict,
            ac_results=ac_results,
            findings=[finding.model_dump() for finding in review_result.findings],
            actor=f"agent:{run.agent_id}",
            idempotency_key=f"run:{run.id}:review-verdict",
        )
    except OrchestrationError as exc:
        orch_cls(db).record_review_failure(
            task_id=run.task_id,
            error=f"Could not record verdict from review result: {exc}",
            actor=f"agent:{run.agent_id}",
            idempotency_key=f"run:{run.id}:review-verdict-failed",
            run_id=run.id,
        )


def _record_review_result_load_failure(
    db: Session,
    run: AgentRun,
    task_id: str,
    exc: ReviewResultLoadError,
    orch_svc_cls: type[TaskOrchestrationService],
) -> str:
    """Fail only the review run and persist the parser's structured detail."""
    error_details = exc.as_dict()
    record_tool_metric(
        tool="review_result",
        source="agent_runner",
        ok=False,
        task_id=task_id,
        error=f"{exc.code}: {exc}",
        payload=error_details,
    )
    run.status = ProcessStatus.FAILED.value
    run.error_message = str(exc)
    orch_svc_cls(db).record_review_failure(
        task_id=task_id,
        error=str(exc),
        actor=f"agent:{run.agent_id}",
        idempotency_key=f"run:{run.id}:review-result-invalid",
        run_id=run.id,
        error_details=error_details,
    )
    return ProcessStatus.FAILED.value


def _normalize_acceptance_criteria(ac: list | str | None) -> list[str]:
    """Convert acceptance_criteria to a list, handling string format."""
    if ac is None:
        return []
    if isinstance(ac, list):
        return ac
    # String format: "AC1: ...\nAC2: ..." - split by newline
    return [line.strip() for line in ac.split("\n") if line.strip()]


def _prepare_review_artifact(
    repo_root: str, task_id: str, acceptance_criteria: list | str | None = None
) -> None:
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

    # Generate template with correct AC count for reviewer to fill
    ac_list = _normalize_acceptance_criteria(acceptance_criteria)
    if ac_list:
        template = {
            "schema_version": "1.0",
            "task_id": task_id,
            "base": "FILL_BASE_REF",
            "head": "FILL_HEAD_REF",
            "ac_results": [
                {
                    "criterion_id": f"ac-{i+1}",
                    "status": "FILL_pass_or_fail",
                    "evidence": ["FILL_evidence"],
                    "finding_ids": [],
                }
                for i in range(len(ac_list))
            ],
            "findings": [],
            "tests_run": [],
            "tests_passed": [],
        }
        template_path = path.replace(".json", ".template.json")
        with open(template_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)


def _review_read_only_git_env() -> tuple[dict[str, str], str]:
    blocked = (
        "commit", "merge", "cherry-pick", "rebase", "reset", "checkout", "push"
    )
    wrapper_dir = tempfile.mkdtemp(prefix="control-tower-review-git-")
    real_git = shutil.which("git") or "/usr/bin/git"
    wrapper = os.path.join(wrapper_dir, "git")
    with open(wrapper, "w", encoding="utf-8") as wrapper_file:
        wrapper_file.write(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do case \"$arg\" in "
            + "|".join(blocked)
            + ") echo \"reviewer git command blocked: $arg\" >&2; exit 128;; esac; done\n"
            + f"exec {shlex.quote(real_git)} \"$@\"\n"
        )
    os.chmod(wrapper, 0o755)
    env = {
        "PATH": f"{wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return env, wrapper_dir


def _update_task_status(
    db: Session,
    task_id: str,
    status: str,
    *,
    result_ref: str | None = None,
    error: str | None = None,
) -> None:
    orch_cls = _get_attr("TaskOrchestrationService", TaskOrchestrationService)
    service = orch_cls(db)
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
    orch_cls = _get_attr("TaskOrchestrationService", TaskOrchestrationService)
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
            orch_cls(db).record_execution_failure(
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


def execute_agent_run(
    run_id: str,
    task_id: str,
    command: str,
    repo_root: str,
    timeout_seconds: int = 900,
) -> int | None:
    """Execute an agent and persist/stream its full lifecycle."""
    session_factory = _get_attr("SessionLocal", SessionLocal)
    process_mgr_cls = _get_attr("ProcessManager", ProcessManager)
    worktree_mgr_cls = _get_attr("WorktreeManager", WorktreeManager)
    coord_svc_cls = _get_attr("CoordinatorService", CoordinatorService)
    orch_svc_cls = _get_attr("TaskOrchestrationService", TaskOrchestrationService)
    emit_event_fn = _get_attr("emit_task_event", emit_task_event)

    db: Session = session_factory()
    cancel_check = _throttled_cancel_check(run_id)
    process_manager = process_mgr_cls(
        timeout_seconds=timeout_seconds,
        cancel_check=cancel_check,
    )
    worktree_manager: WorktreeManager | None = None
    worktree_path: str | None = None
    review_git_dir: str | None = None
    exec_cwd = repo_root
    mcp_cleanup_paths: list[str] = []

    try:
        run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
        if run is None:
            logger.error("AgentRun %s does not exist; discarding message", run_id)
            return None

        if run.status in {"success", "timeout", "cancelled"} or (
            run.status == "failed" and run.attempt >= run.max_attempts
        ):
            logger.info("Ignoring duplicate delivery for terminal run %s", run_id)
            return run.exit_code

        brake = orch_svc_cls(db).check_brakes(
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
        coord_svc_cls(db).get_or_create_task_session(task_id)
        is_review_run = run.kind == "review"
        is_review_task = task is not None and (
            is_review_run or _is_review_task(task)
        )
        if is_review_task:
            _prepare_review_artifact(repo_root, task_id, task.acceptance_criteria)
        attempt = _current_attempt(run)
        run.status = "running"
        run.attempt = attempt
        run.started_at = datetime.now(timezone.utc)
        run.completed_at = None
        run.exit_code = None
        run.pid = None
        run.error_message = None

        base_ref = _run_base_ref(run.result_ref) or _parse_result_ref(repo_root)
        if base_ref is None:
            run.status = "failed"
            run.error_message = "Could not determine repository HEAD before execution"
            db.commit()
            _emit_decision_event(
                db,
                task_id=task_id,
                event_type="run_failed",
                payload={
                    "run_id": run_id,
                    "error": run.error_message,
                    "exit_code": -1,
                },
            )
            orch_svc_cls(db).record_execution_failure(
                task_id=task_id,
                error=run.error_message,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:missing-base",
                run_id=run.id,
            )
            _nudge_driver(task_id, "run_agent_completed")
            return None

        worktree_ref = base_ref
        if is_review_run:
            review_base, separator, review_head = (task.result_ref or "").partition("..")
            if not separator or not review_base or not review_head:
                raise ReviewResultLoadError(
                    "invalid_review_range",
                    review_result_path(repo_root, task_id),
                    "Review task has no valid committed base..head range",
                )
            if _git_ref(repo_root, review_head) is None:
                raise ReviewResultLoadError(
                    "invalid_review_head",
                    review_result_path(repo_root, task_id),
                    "Review head is not a valid commit",
                    head=review_head,
                )
            worktree_ref = review_head

        if _use_worktree():
            worktree_manager = worktree_mgr_cls(repo_root)
            try:
                worktree_path = worktree_manager.create(run.id, worktree_ref)
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
        started_at = datetime.now(timezone.utc)
        run.started_at = started_at
        db.refresh(run)
        if run.status == "cancelled":
            logger.info("Run %s was cancelled before execution started", run_id)
            _nudge_driver(task_id, "run_agent_completed")
            return None

        run.status = "running"
        run.attempt = attempt
        run.started_at = started_at
        db.commit()

        def record_pid(pid: int) -> None:
            run.pid = pid
            db.commit()
            emit_event_fn(
                task_id=task_id,
                event_type="running",
                payload={"run_id": run_id, "pid": pid},
                db=db,
            )

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
        # Allocate 2 sequences atomically for run.started + llm.requested
        event_seq = _next_agent_event_seq(db, run_id, count=2)
        result: ProcessResult | None = None

        _record_agent_event(
            db, run_id, event_seq, "run.started", {"cli": run.cli, "attempt": attempt}
        )
        event_seq += 1
        _record_agent_event(db, run_id, event_seq, "llm.requested", {"command": command})
        event_seq += 1
        db.commit()

        from app.services.mcp_attach import attach_mcp

        mcp_cleanup_paths = []
        if not run.cli:
            logger.warning(
                "Run %s has no CLI recorded; MCP attachment defaults to claude",
                run_id,
            )
        try:
            exec_command, mcp_env, mcp_cleanup_paths = attach_mcp(
                cli=run.cli or "claude",
                command=command,
                workdir=exec_cwd,
                task_id=task_id,
                role="executor",
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            logger.exception("Failed to attach MCP for run %s", run_id)
            exec_command = command
            mcp_env = {}
            try:
                emit_event_fn(
                    task_id,
                    "mcp_attach_failed",
                    payload={"run_id": run_id, "cli": run.cli},
                    db=db,
                    kind="info",
                )
                db.commit()
            except Exception:
                logger.exception("Failed to emit mcp_attach_failed for run %s", run_id)

        if is_review_run:
            process_env, review_git_dir = _review_read_only_git_env()
            if mcp_env:
                process_env.update(mcp_env)
        else:
            process_env = mcp_env or None

        for output in process_manager.run_with_streaming(
            exec_command, exec_cwd, env=process_env
        ):
            if isinstance(output, ProcessResult):
                result = output
                break

            line_count += 1
            total_bytes += len(output.encode("utf-8"))
            chunk_buffer.append(output)
            explicit_result_ref = (
                _extract_explicit_result_ref(output) or explicit_result_ref
            )
            event_seq = _record_vendor_output(
                db, run_id, run.cli, line_count - 1, event_seq, output
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

            publish_line(run_id, output, line_index=line_count)

            if len(chunk_buffer) >= _get_attr("OUTPUT_CHUNK_LINES", OUTPUT_CHUNK_LINES):
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

        # Allocate seq for run.completed event
        completed_seq = _next_agent_event_seq(db, run_id, count=1)
        _record_agent_event(
            db,
            run_id,
            completed_seq,
            "run.completed",
            {
                "status": result.status.value,
                "exit_code": result.exit_code,
                "error": result.error,
            },
        )

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

        # Parse only after a terminal attempt.  A retry must not create a
        # second usage row for the same AgentRun, and malformed vendor output
        # is intentionally a no-op.
        if result.status != ProcessStatus.CANCELLED:
            raw_output = "\n".join(
                event.raw_output
                for event in db.query(VendorRawEvent)
                .filter(VendorRawEvent.run_id == run.id)
                .order_by(VendorRawEvent.seq)
                .all()
            )
            # JSON mode may place the executor's final RESULT_REF inside the
            # result field of one pretty-printed object, so inspect the full
            # persisted output in addition to the per-line streaming path.
            explicit_result_ref = (
                _extract_explicit_result_ref(raw_output) or explicit_result_ref
            )
            _record_cli_usage(db, run, run.cli, raw_output)

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
                    effective_status = _record_review_result_load_failure(
                        db, run, task_id, exc, orch_svc_cls
                    )
                else:
                    run.result_ref = os.path.relpath(
                        review_result_path(repo_root, review_result.task_id),
                        repo_root,
                    )
                    db.flush()
                    _submit_review_verdict(db, run, review_result)
            elif is_review_task and task is not None:
                review_result = load_review_result(
                    repo_root,
                    task_id,
                    task.acceptance_criteria or [],
                )
                run.result_ref = os.path.relpath(
                    review_result_path(repo_root, review_result.task_id), repo_root
                )
            elif (explicit_result_ref or "").strip().lower() == "none":
                head_now = _git_ref(exec_cwd, "HEAD")
                base_now = _git_ref(exec_cwd, base_ref)
                if head_now and base_now and head_now != base_now:
                    err = (
                        "Executor declared 'RESULT_REF: none' but the "
                        "worktree has committed changes"
                    )
                    run.status = ProcessStatus.FAILED.value
                    effective_status = ProcessStatus.FAILED.value
                    run.error_message = err
                    orch_svc_cls(db).record_execution_failure(
                        task_id=task_id,
                        error=err,
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:none-with-commits",
                        run_id=run.id,
                    )
                else:
                    try:
                        run.result_ref = "no-commit"
                        orch_svc_cls(db).complete_no_commit_task(
                            task_id=task_id,
                            actor=f"agent:{run.agent_id}",
                            run_id=run.id,
                        )
                    except OrchestrationError as exc:
                        run.status = ProcessStatus.FAILED.value
                        effective_status = ProcessStatus.FAILED.value
                        run.error_message = str(exc)
                        orch_svc_cls(db).record_execution_failure(
                            task_id=task_id,
                            error=str(exc),
                            actor=f"agent:{run.agent_id}",
                            idempotency_key=f"run:{run.id}:no-commit-rejected",
                            run_id=run.id,
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
                    orch_svc_cls(db).record_execution_failure(
                        task_id=task_id,
                        error=no_change_error,
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:no-committed-changes",
                        run_id=run.id,
                    )
                else:
                    run.result_ref = result_ref
                    orch_svc_cls(db).record_execution_success(
                        task_id=task_id,
                        result_ref=run.result_ref,
                        actor=f"agent:{run.agent_id}",
                        idempotency_key=f"run:{run.id}:execution-success",
                        run_id=run.id,
                    )
                    from app.db.models import Task
                    task_obj = db.get(Task, task_id)
                    if task_obj and task_obj.project and repo_root:
                        record_commit_event(
                            db,
                            task_obj.project,
                            repo_root,
                            commit_sha=run.result_ref,
                        )
        elif is_review_run:
            orch_svc_cls(db).record_review_failure(
                task_id=task_id,
                error=result.error or result.status.value,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:review-{result.status.value}",
                run_id=run.id,
            )
        else:
            orch_svc_cls(db).record_execution_failure(
                task_id=task_id,
                error=result.error or result.status.value,
                actor=f"agent:{run.agent_id}",
                idempotency_key=f"run:{run.id}:execution-{result.status.value}",
                run_id=run.id,
            )
        _record_run_resource_usage(db, run)
        db.commit()
        effective_error = run.error_message or result.error
        if effective_status == ProcessStatus.COMPLETED.value:
            emit_event_fn(
                task_id=task_id,
                event_type="done",
                payload={
                    "run_id": run_id,
                    "result_ref": run.result_ref,
                    "exit_code": result.exit_code,
                },
                db=db,
            )
        elif effective_status != ProcessStatus.CANCELLED.value:
            _emit_decision_event(
                db,
                task_id=task_id,
                event_type="run_failed",
                payload={
                    "run_id": run_id,
                    "error": effective_error,
                    "exit_code": result.exit_code,
                },
            )
        _nudge_driver(task_id, "run_agent_completed")

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
        raise
    except Exception as exc:
        logger.exception("Agent run %s failed unexpectedly", run_id)
        db.rollback()
        should_retry = _record_unexpected_failure(db, run_id, task_id, exc)
        if should_retry:
            publish_status(run_id, "retrying", error=str(exc))
            raise
        _emit_decision_event(
            db,
            task_id=task_id,
            event_type="run_failed",
            payload={"run_id": run_id, "error": str(exc)},
        )
        publish_status(run_id, "failed", error=str(exc))
        _nudge_driver(task_id, "run_agent_completed")
        return None
    finally:
        process_manager.terminate()
        _cleanup_mcp_config(mcp_cleanup_paths)
        if worktree_manager is not None and worktree_path is not None:
            worktree_manager.remove(worktree_path)
        if review_git_dir is not None:
            shutil.rmtree(review_git_dir, ignore_errors=True)
        db.close()
