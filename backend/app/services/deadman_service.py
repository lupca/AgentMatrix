"""Deadman monitor: one signal about the SYSTEM, not one per task (CTV2-1400).

Silence on Telegram used to mean two different things that looked identical:
everything is fine, or the server has been down for an hour. 2026-08-04 the
server hung twice with nobody around to notice. This module turns "no news"
into a positive signal.

CTV2-1401 -- the first version asked the wrong question. It swept every task
whose status was not done/cancelled and fired one event per stalled task. On
the first sweep after a restart that meant 116 tasks crossed the threshold at
once and the human got 232 Telegram messages: precisely the noise this whole
feature exists to cut.

The question deadman answers is singular: *is the system still alive?* One
question about the whole system, not N questions about N tasks. And a task
parked in `todo` for a week is not a stall -- it is a backlog. Nobody promised
to move it, so its stillness says nothing about whether the machine is
breathing. Only work the system has actually accepted -- a run that is queued
or running -- carries that promise, so only that work can be overdue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AgentRun, Setting, TaskEvent
from app.services.task_event_service import emit_task_event

#: Runs that mean "the system took this on and owes an outcome".
IN_FLIGHT_RUN_STATUSES = ("queued", "running")


def get_no_progress_minutes(db: Session) -> int:
    """Setting-table override of DEADMAN_NO_PROGRESS_MINUTES, default 30."""
    row = db.get(Setting, "deadman_no_progress_minutes")
    if row is None:
        return max(1, settings.DEADMAN_NO_PROGRESS_MINUTES)
    try:
        return max(1, int(row.value))
    except (TypeError, ValueError, InvalidOperation):
        return max(1, settings.DEADMAN_NO_PROGRESS_MINUTES)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def last_progress_at(db: Session) -> datetime | None:
    """Newest heartbeat of the system as a whole.

    Anything that moves is progress, so this is the max over both the event
    log and the run table -- a long-running agent that has not emitted an
    event yet still counts as alive via its run row.
    """
    newest_event = db.query(func.max(TaskEvent.created_at)).scalar()
    newest_run = db.query(func.max(AgentRun.started_at)).scalar()
    candidates = [ts for ts in (_aware(newest_event), _aware(newest_run)) if ts is not None]
    return max(candidates) if candidates else None


def in_flight_run_count(db: Session) -> int:
    """Runs the system owes an outcome for."""
    return (
        db.query(func.count(AgentRun.id))
        .filter(AgentRun.status.in_(IN_FLIGHT_RUN_STATUSES))
        .scalar()
        or 0
    )


def system_stalled(
    db: Session, *, threshold_minutes: int, now: datetime | None = None
) -> tuple[bool, int, datetime | None]:
    """Is there accepted work outstanding while nothing at all has moved?

    Returns (stalled, in_flight_count, last_progress). Both halves are
    required: idle-and-quiet is healthy, busy-and-quiet is not.
    """
    now = now or datetime.now(timezone.utc)
    in_flight = in_flight_run_count(db)
    if in_flight == 0:
        # Nothing outstanding. Quiet is the correct state, not a symptom.
        return False, 0, last_progress_at(db)
    progress = last_progress_at(db)
    if progress is None:
        return True, in_flight, None
    return progress <= now - timedelta(minutes=threshold_minutes), in_flight, progress


def _last_deadman_at(db: Session) -> datetime | None:
    row = (
        db.query(TaskEvent)
        .filter(TaskEvent.event_type == "deadman")
        .order_by(TaskEvent.id.desc())
        .first()
    )
    return _aware(row.created_at) if row is not None else None


def fire_deadman_events(
    db: Session, *, threshold_minutes: int | None = None, now: datetime | None = None
) -> list[TaskEvent]:
    """Emit at most ONE system-wide `deadman` event per stall.

    Returns a list to keep the caller's contract, but it holds either zero or
    one event -- never one per task.
    """
    now = now or datetime.now(timezone.utc)
    minutes = threshold_minutes if threshold_minutes is not None else get_no_progress_minutes(db)

    stalled, in_flight, progress = system_stalled(db, threshold_minutes=minutes, now=now)
    if not stalled:
        return []

    # Do not repeat the same stall. Only once the system has actually moved
    # again -- progress newer than the warning -- may a fresh one fire.
    last_deadman = _last_deadman_at(db)
    if last_deadman is not None and (progress is None or progress <= last_deadman):
        return []

    quiet_minutes = int((now - progress).total_seconds() // 60) if progress is not None else minutes
    event = emit_task_event(
        task_id=None,
        event_type="deadman",
        payload={
            "in_flight_runs": in_flight,
            "no_progress_minutes": quiet_minutes,
            "last_progress_at": progress.isoformat() if progress is not None else None,
            "reason": (
                f"{in_flight} run(s) in flight but nothing has moved for "
                f"{quiet_minutes} minutes -- the system may be stuck or down."
            ),
            "next": "Kiểm tra backend/worker còn sống không; xem backend.log.",
        },
        db=db,
        kind="decision",
    )
    return [event]
