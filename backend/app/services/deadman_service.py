"""Deadman monitor: fire exactly one `deadman` event per stall (CTV2-1400).

Silence on Telegram used to mean two different things that looked identical:
everything is fine, or the server has been down for an hour. 2026-08-04 the
server hung twice with nobody around to notice. This module's only job is to
turn "no news" into a positive signal when a task has unfinished work and
hasn't moved in N minutes -- and to send that signal exactly once per stall,
because a repeating reminder is itself the kind of noise this whole feature
exists to cut.

Not-repeating is enforced by comparing `Task.updated_at` (a proxy for "did
anything happen to this task") against the most recent `deadman` TaskEvent
for it: if nothing has touched the task since the last deadman fired, it does
not fire again. Once something moves the task forward (a new run, a gate
decision, anything that bumps `updated_at`) and it then stalls again, a fresh
deadman event is allowed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Setting, Task, TaskEvent
from app.services.task_event_service import emit_task_event


def get_no_progress_minutes(db: Session) -> int:
    """Setting-table override of DEADMAN_NO_PROGRESS_MINUTES, default 30."""
    row = db.get(Setting, "deadman_no_progress_minutes")
    if row is None:
        return max(1, settings.DEADMAN_NO_PROGRESS_MINUTES)
    try:
        return max(1, int(row.value))
    except (TypeError, ValueError, InvalidOperation):
        return max(1, settings.DEADMAN_NO_PROGRESS_MINUTES)


def find_stalled_tasks(
    db: Session, *, threshold_minutes: int, now: datetime | None = None
) -> list[Task]:
    """Unfinished tasks (`status` not in done/cancelled) untouched for
    ``threshold_minutes``, that have not already fired a deadman event since
    their last update (i.e. would be a repeat, not a new stall)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=threshold_minutes)
    candidates = (
        db.query(Task)
        .filter(Task.status.notin_(["done", "cancelled"]), Task.updated_at <= cutoff)
        .order_by(Task.id)
        .all()
    )
    stalled: list[Task] = []
    for task in candidates:
        task_updated_at = task.updated_at
        if task_updated_at is not None and task_updated_at.tzinfo is None:
            task_updated_at = task_updated_at.replace(tzinfo=timezone.utc)
        last_deadman = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task.id, TaskEvent.event_type == "deadman")
            .order_by(TaskEvent.id.desc())
            .first()
        )
        if last_deadman is not None and task_updated_at is not None:
            last_deadman_at = last_deadman.created_at
            if last_deadman_at.tzinfo is None:
                last_deadman_at = last_deadman_at.replace(tzinfo=timezone.utc)
            if last_deadman_at >= task_updated_at:
                # Nothing has happened to the task since we already warned
                # about this exact stall -- do not repeat.
                continue
        stalled.append(task)
    return stalled


def fire_deadman_events(
    db: Session, *, threshold_minutes: int | None = None, now: datetime | None = None
) -> list[TaskEvent]:
    """Emit one `deadman` TaskEvent per newly-stalled task. Returns the events."""
    now = now or datetime.now(timezone.utc)
    minutes = threshold_minutes if threshold_minutes is not None else get_no_progress_minutes(db)
    events: list[TaskEvent] = []
    for task in find_stalled_tasks(db, threshold_minutes=minutes, now=now):
        task_updated_at = task.updated_at
        if task_updated_at is not None and task_updated_at.tzinfo is None:
            task_updated_at = task_updated_at.replace(tzinfo=timezone.utc)
        no_progress_minutes = (
            int((now - task_updated_at).total_seconds() // 60)
            if task_updated_at is not None
            else minutes
        )
        event = emit_task_event(
            task_id=task.id,
            event_type="deadman",
            payload={
                "task_id": task.id,
                "status": task.status,
                "no_progress_minutes": no_progress_minutes,
                "reason": f"Task {task.id} has had no progress for over {minutes} minutes.",
            },
            db=db,
            kind="decision",
        )
        events.append(event)
    return events
