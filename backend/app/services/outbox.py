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


def _publish_one(db: Session, event: OutboxEvent, now: datetime) -> None:
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
