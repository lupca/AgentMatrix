"""Transactional outbox for reliable AgentRun dispatch (CTV2-205).

Previously a run was queued by: INSERT AgentRun -> COMMIT -> run_agent.send().
A crash between the commit and the send left the run "queued" forever with
no Dramatiq message behind it. `TaskOrchestrationService._apply_gate` now
writes an `OutboxEvent(event_type="run_requested")` in the very same
transaction/commit as the `AgentRun` (see `record_run_requested`); the
existing synchronous call sites still call `run_agent.send()` immediately
afterwards as a low-latency fast path, but that is no longer the only way
the message gets sent. `publish_pending_events` is the backstop: it is safe
to call any number of times for the same event because it treats
`AgentRun.dramatiq_message_id` as the source of truth for "already
enqueued".
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.db.models import AgentRun, OutboxEvent, Project, Task

logger = logging.getLogger(__name__)

# A publish attempt is retried with exponential backoff (2^attempts seconds,
# capped) rather than immediately -- a transient broker/network blip
# shouldn't be hammered every poll tick.
MAX_PUBLISH_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2
BACKOFF_MAX_SECONDS = 300

# An AgentRun still "queued" with no dramatiq_message_id after this long has
# outlived any reasonable publish delay and is treated as orphaned even if no
# outbox row currently tracks it (e.g. the row was hand-deleted, or predates
# this feature).
ORPHAN_RUN_AGE_SECONDS = 60

# A "running" AgentRun younger than this is never reaped, however dead its
# recorded PID looks -- it guards a still-legitimate run whose PID hasn't
# been persisted/observed yet, and it's the mitigation for PID reuse: the OS
# can hand a dead run's old PID to an unrelated new process, so a check run
# immediately after the process died could misjudge liveness either way.
# Waiting this long makes a same-tick collision implausible.
REAP_RUN_MIN_AGE_SECONDS = max(1, int(os.getenv("OUTBOX_REAP_MIN_AGE_SECONDS", "120")))


def record_run_requested(db: Session, run: AgentRun, repo_root: str) -> OutboxEvent:
    """Write the outbox row for `run` in the caller's current transaction.

    Must be called from inside the same unit of work that inserts `run`
    (i.e. before that transaction commits) -- that atomicity is the entire
    point of the pattern.
    """
    event = OutboxEvent(
        event_type="run_requested",
        payload={
            "run_id": run.id,
            "task_id": run.task_id,
            "command": run.command,
            "repo_root": repo_root,
            "timeout_seconds": run.timeout_seconds,
        },
    )
    db.add(event)
    return event


def record_graph_rebuild_requested(
    db: Session, project_id: str, repo_root: str, commit_sha: str | None = None
) -> OutboxEvent | None:
    """Write the outbox row for a graph rebuild request in the caller's transaction.

    Moves project.graph_status to 'stale' (if idle or fresh) and queues an outbox event.
    """
    project = db.get(Project, project_id)
    if project is not None:
        if project.graph_status != "building":
            project.graph_status = "stale"

    existing = (
        db.query(OutboxEvent)
        .filter(
            OutboxEvent.event_type == "graph_rebuild_requested",
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dead_letter.is_(False),
        )
        .all()
    )
    for evt in existing:
        if (evt.payload or {}).get("project_id") == project_id:
            return evt

    event = OutboxEvent(
        event_type="graph_rebuild_requested",
        payload={
            "project_id": project_id,
            "repo_root": repo_root,
            "commit_sha": commit_sha,
        },
    )
    db.add(event)
    return event


def record_commit_event(
    db: Session, project_id: str, repo_root: str, commit_sha: str | None = None
) -> OutboxEvent | None:
    """Trigger incremental graph rebuild outbox event on code commit."""
    return record_graph_rebuild_requested(db, project_id, repo_root, commit_sha)


def rebuild_graph_incremental_sync(repo_root: str, timeout: float = 60.0) -> dict[str, Any]:
    import asyncio
    import concurrent.futures
    from app.services.graph_client import rebuild_graph_incremental

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(
                lambda: asyncio.run(rebuild_graph_incremental(repo_root, timeout))
            ).result()
    else:
        return asyncio.run(rebuild_graph_incremental(repo_root, timeout))


def publish_pending_events(
    db: Session, *, limit: int = 50, now: datetime | None = None
) -> dict[str, int]:
    """Poll unpublished, non-dead-lettered events and enqueue their run.

    Returns a summary count; call sites (the outbox_publisher worker, tests)
    use it to decide whether to log. Commits internally so each event's
    outcome is durable even if a later event in the same batch raises.
    """
    now = now or datetime.now(timezone.utc)
    counts = {"published": 0, "deferred": 0, "dead_lettered": 0}

    events = (
        db.query(OutboxEvent)
        .filter(
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dead_letter.is_(False),
        )
        .order_by(OutboxEvent.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for event in events:
        if not _backoff_elapsed(event, now):
            counts["deferred"] += 1
            continue
        _publish_one(db, event, now)
        db.commit()
        if event.dead_letter:
            counts["dead_lettered"] += 1
        elif event.published_at is not None:
            counts["published"] += 1
        else:
            counts["deferred"] += 1
    return counts


def _backoff_elapsed(event: OutboxEvent, now: datetime) -> bool:
    if event.attempts == 0 or event.last_attempted_at is None:
        return True
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS**event.attempts)
    last_attempted_at = event.last_attempted_at
    if last_attempted_at.tzinfo is None:
        # SQLite (unlike Postgres) drops tzinfo on round-trip even though the
        # column is DateTime(timezone=True); everything this module writes is
        # already UTC, so it's safe to just reattach it here.
        last_attempted_at = last_attempted_at.replace(tzinfo=timezone.utc)
    return (now - last_attempted_at).total_seconds() >= delay


def _publish_graph_rebuild(db: Session, event: OutboxEvent, now: datetime) -> None:
    payload = event.payload or {}
    project_id = payload.get("project_id")
    repo_root = payload.get("repo_root")

    project = db.get(Project, project_id) if project_id else None
    if project is not None:
        project.graph_status = "building"
        db.flush()

    event.attempts += 1
    event.last_attempted_at = now

    try:
        if repo_root:
            rebuild_graph_incremental_sync(repo_root)
        if project is not None:
            project.graph_status = "fresh"
        event.published_at = now
        event.last_error = None
    except Exception as exc:
        logger.exception("outbox: graph rebuild failed for project %s: %s", project_id, exc)
        event.last_error = str(exc)[:2000]
        if project is not None:
            project.graph_status = "stale"
        if event.attempts >= MAX_PUBLISH_ATTEMPTS:
            event.dead_letter = True


def _publish_one(db: Session, event: OutboxEvent, now: datetime) -> None:
    if event.event_type == "graph_rebuild_requested":
        _publish_graph_rebuild(db, event, now)
        return
    # Deferred: app.workers.agent_runner imports app.services.task_orchestration
    # at module scope, which (via this module) would otherwise be a cycle.
    from app.workers.agent_runner import run_agent

    payload = event.payload or {}
    run = db.get(AgentRun, payload.get("run_id"))
    if run is None or run.status not in {"queued", "running"}:
        # No longer actionable: the run was never created (shouldn't happen),
        # or it already left "queued" through some other path (a synchronous
        # dispatch failure already called record_dispatch_queue_failure, or
        # it was cancelled) -- either way there's nothing left to publish.
        event.published_at = now
        return
    if run.dramatiq_message_id:
        # The synchronous fast path (run_agent.send() right after commit)
        # already succeeded; just close out the outbox row.
        event.published_at = now
        return

    event.attempts += 1
    event.last_attempted_at = now
    try:
        message = run_agent.send(
            run.id,
            run.task_id,
            run.command,
            str(payload.get("repo_root", "")),
            int(payload.get("timeout_seconds") or run.timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001 - broker errors are caller-defined
        event.last_error = str(exc)[:2000]
        logger.warning(
            "outbox: publish failed for run %s (attempt %s/%s): %s",
            run.id,
            event.attempts,
            MAX_PUBLISH_ATTEMPTS,
            exc,
        )
        if event.attempts >= MAX_PUBLISH_ATTEMPTS:
            _dead_letter(db, event, run)
        return

    message_id = getattr(message, "message_id", None)
    if message_id:
        run.dramatiq_message_id = str(message_id)
    event.published_at = now
    event.last_error = None


def _dead_letter(db: Session, event: OutboxEvent, run: AgentRun) -> None:
    """Give up on `event` and escalate its run the same way a synchronous
    dispatch-queue failure would, rather than retrying forever."""
    # Deferred to avoid a task_orchestration <-> outbox import cycle
    # (task_orchestration imports record_run_requested at module scope).
    from app.services.task_orchestration import TaskOrchestrationService

    event.dead_letter = True
    service = TaskOrchestrationService(db)
    try:
        service.record_dispatch_queue_failure(
            run_id=run.id,
            error=(
                f"outbox publish exhausted after {event.attempts} attempts: "
                f"{event.last_error}"
            ),
            actor="system:outbox-publisher",
            idempotency_key=f"outbox:{event.id}:dead-letter",
        )
    except Exception:
        logger.exception("outbox: dead-letter handling failed for run %s", run.id)


def reconcile_orphaned_runs(
    db: Session,
    *,
    older_than_seconds: int = ORPHAN_RUN_AGE_SECONDS,
    now: datetime | None = None,
) -> int:
    """Recover AgentRun rows stuck `queued` with no message and no tracking event.

    This is the last-resort backstop: normal operation always has an
    unpublished `OutboxEvent` for a not-yet-sent run, so `publish_pending_events`
    alone is enough. This job exists for the case where a run's outbox row
    was never written, or was lost by an out-of-band mistake (e.g. someone
    manually deleting outbox rows). Re-enqueuing a fresh
    `OutboxEvent` puts the run back on the normal publish path instead of
    of requiring this function to duplicate its retry/backoff/dead-letter logic.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=older_than_seconds)
    orphans = (
        db.query(AgentRun)
        .filter(
            AgentRun.status == "queued",
            AgentRun.dramatiq_message_id.is_(None),
            AgentRun.queued_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )
    if not orphans:
        return 0

    tracked_run_ids = {
        (row.payload or {}).get("run_id")
        for row in db.query(OutboxEvent).filter(
            OutboxEvent.event_type == "run_requested",
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dead_letter.is_(False),
        )
    }

    reconciled = 0
    for run in orphans:
        if run.id in tracked_run_ids:
            continue
        task = db.get(Task, run.task_id)
        project = db.get(Project, task.project) if task is not None else None
        repo_root = project.repo_root if project is not None else None
        if not repo_root:
            logger.warning(
                "outbox: cannot reconcile orphaned run %s, no repo_root for task %s",
                run.id,
                run.task_id,
            )
            continue
        db.add(
            OutboxEvent(
                event_type="run_requested",
                payload={
                    "run_id": run.id,
                    "task_id": run.task_id,
                    "command": run.command,
                    "repo_root": repo_root,
                    "timeout_seconds": run.timeout_seconds,
                },
            )
        )
        reconciled += 1
        logger.warning("outbox: reconciled orphaned run %s (task %s)", run.id, run.task_id)

    db.commit()
    return reconciled


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else (e.g. PID reused by a root
        # process) -- that's still "alive" for our purposes.
        return True
    return True


def reap_dead_running_runs(
    db: Session,
    *,
    min_age_seconds: int = REAP_RUN_MIN_AGE_SECONDS,
    now: datetime | None = None,
) -> int:
    """Fail AgentRun rows stuck `running` behind a worker process that died.

    Normal completion (success, failure, or retry) always moves a run out of
    "running" from inside the actor that started it -- `run_agent` reads the
    subprocess to completion itself. A run only stays "running" forever when
    the *worker process* running that actor was killed out from under it
    (OOM, host restart, ...), not the CLI subprocess dying on its own. Its
    last recorded `pid` is then a dead process: `os.kill(pid, 0)` raises
    `ProcessLookupError`. `min_age_seconds` (see `REAP_RUN_MIN_AGE_SECONDS`)
    protects a genuinely young/live run from a false reap.

    Like `reconcile_orphaned_runs`, this locks candidate rows with
    `skip_locked=True` so concurrent callers (multiple worker processes
    running this same poll loop) never double-reap the same run.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=min_age_seconds)
    candidates = (
        db.query(AgentRun)
        .filter(
            AgentRun.status == "running",
            AgentRun.pid.isnot(None),
            AgentRun.started_at.isnot(None),
            AgentRun.started_at < cutoff,
        )
        .with_for_update(skip_locked=True)
        .all()
    )

    reaped = 0
    for run in candidates:
        if _process_is_alive(run.pid):
            continue
        if _reap_run(db, run, now):
            reaped += 1
    return reaped


def _reap_run(db: Session, run: AgentRun, now: datetime) -> bool:
    # Deferred to avoid a task_orchestration <-> outbox import cycle, same as
    # `_dead_letter` above.
    from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService

    pid = run.pid
    error = f"reaped: worker process {pid} is dead"
    run.status = "failed"
    run.error_message = error
    run.completed_at = now
    run.pid = None
    # Committed on its own, ahead of the task-level transition below, so the
    # concurrency slot (brakes count status in {queued, running}) is freed
    # even if that next step fails.
    db.commit()

    service = TaskOrchestrationService(db)
    record_failure = (
        service.record_review_failure
        if run.kind == "review"
        else service.record_execution_failure
    )
    try:
        record_failure(
            task_id=run.task_id,
            error=error,
            actor="system:reaper",
            idempotency_key=f"reap:{run.id}",
            run_id=run.id,
        )
    except OrchestrationError:
        logger.exception(
            "outbox: reaped run %s but could not transition task %s out of its "
            "in-flight status",
            run.id,
            run.task_id,
        )
    logger.warning(
        "outbox: reaped dead run %s (task %s, pid %s)", run.id, run.task_id, pid
    )
    return True
