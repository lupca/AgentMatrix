"""TaskEventService for managing task state change events (CTV2-114)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import SessionEventCursor, TaskEvent


DECISION_EVENT_TYPES = frozenset({"gate_pending", "run_failed", "escalated"})


class _EmitDescriptor:
    """Descriptor enabling TaskEventService.emit to work on both class and instance."""

    def __get__(self, instance: TaskEventService | None, owner: type[TaskEventService]):
        if instance is not None:
            def instance_emit(
                task_id: str,
                event_type: str,
                payload: dict[str, Any] | None = None,
                db: Session | None = None,
                kind: str | None = None,
            ) -> TaskEvent:
                return emit_task_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload=payload,
                    kind=kind,
                    db=db or instance.db,
                )

            return instance_emit
        else:
            def class_emit(
                task_id: str,
                event_type: str,
                payload: dict[str, Any] | None = None,
                db: Session | None = None,
                kind: str | None = None,
            ) -> TaskEvent:
                return emit_task_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload=payload,
                    kind=kind,
                    db=db,
                )

            return class_emit


class TaskEventService:
    """Service for emitting, querying, and managing TaskEvents."""

    emit = _EmitDescriptor()

    def __init__(self, db: Session | None = None):
        self.db = db

    def get_events(
        self,
        task_id: str | None = None,
        since: datetime | None = None,
        event_types: list[str] | None = None,
        limit: int = 100,
        db: Session | None = None,
    ) -> list[TaskEvent]:
        target_db = db or self.db
        if target_db is None:
            raise ValueError("Database session required for get_events")
        return get_task_events(
            db=target_db,
            task_id=task_id,
            since=since,
            event_types=event_types,
            limit=limit,
        )

    def mark_consumed(
        self,
        event_ids: list[int],
        db: Session | None = None,
    ) -> int:
        target_db = db or self.db
        if target_db is None:
            raise ValueError("Database session required for mark_consumed")
        return mark_task_events_consumed(db=target_db, event_ids=event_ids)

    def claim_event(self, event_id: int, session_id: str, db: Session | None = None) -> bool:
        target_db = db or self.db
        if target_db is None:
            raise ValueError("Database session required for claim_event")
        return claim_event(event_id, session_id, target_db)

    def get_digest(
        self, session_id: str, limit: int = 100, db: Session | None = None
    ) -> list[TaskEvent]:
        target_db = db or self.db
        if target_db is None:
            raise ValueError("Database session required for get_digest")
        return get_digest(session_id, limit, target_db)

    def advance_cursor(self, session_id: str, event_id: int, db: Session | None = None) -> None:
        target_db = db or self.db
        if target_db is None:
            raise ValueError("Database session required for advance_cursor")
        advance_cursor(session_id, event_id, target_db)


def emit_task_event(
    task_id: str | None,
    event_type: str,
    payload: dict[str, Any] | None = None,
    db: Session | None = None,
    kind: str | None = None,
) -> TaskEvent:
    """Record a new task event in the database."""
    if payload is None:
        payload = {}
    if kind is None:
        kind = "decision" if event_type in DECISION_EVENT_TYPES else "info"

    own_session = False
    if db is None:
        from app.db.base import SessionLocal

        db = SessionLocal()
        own_session = True

    try:
        event = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            kind=kind,
            payload=payload,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event
    except Exception:
        if own_session:
            db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def get_task_events(
    db: Session,
    task_id: str | None = None,
    since: datetime | None = None,
    event_types: list[str] | None = None,
    limit: int = 100,
) -> list[TaskEvent]:
    """Retrieve task events with optional filters."""
    query = db.query(TaskEvent)

    if task_id:
        query = query.filter(TaskEvent.task_id == task_id)

    if since:
        query = query.filter(TaskEvent.created_at > since)

    if event_types:
        query = query.filter(TaskEvent.event_type.in_(event_types))

    return query.order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc()).limit(limit).all()


def mark_task_events_consumed(
    db: Session,
    event_ids: list[int],
    consumed_at: datetime | None = None,
) -> int:
    """Mark specified task events as consumed."""
    if not event_ids:
        return 0

    if consumed_at is None:
        consumed_at = datetime.now(timezone.utc)

    updated_count = (
        db.query(TaskEvent)
        .filter(TaskEvent.id.in_(event_ids))
        .update({TaskEvent.consumed_at: consumed_at}, synchronize_session=False)
    )
    db.commit()
    return updated_count


def claim_event(event_id: int, session_id: str, db: Session) -> bool:
    """Atomically claim a decision event for one session."""
    result = db.execute(
        update(TaskEvent)
        .where(
            TaskEvent.id == event_id,
            TaskEvent.kind == "decision",
            TaskEvent.claimed_by_session_id.is_(None),
        )
        .values(claimed_by_session_id=session_id)
    )
    db.commit()
    return result.rowcount == 1


def get_digest(session_id: str, limit: int, db: Session) -> list[TaskEvent]:
    """Return informational events newer than this session's digest cursor."""
    cursor = db.get(SessionEventCursor, session_id)
    if cursor is None:
        cursor = SessionEventCursor(session_id=session_id, last_digest_event_id=0)
        db.add(cursor)
        db.flush()

    events = (
        db.query(TaskEvent)
        .filter(
            TaskEvent.kind == "info",
            TaskEvent.id > cursor.last_digest_event_id,
        )
        .order_by(TaskEvent.id.asc())
        .limit(limit)
        .all()
    )
    db.commit()
    return events


def advance_cursor(session_id: str, event_id: int, db: Session) -> None:
    """Advance a session cursor without allowing it to move backwards."""
    cursor = db.get(SessionEventCursor, session_id)
    if cursor is None:
        cursor = SessionEventCursor(session_id=session_id, last_digest_event_id=event_id)
        db.add(cursor)
    elif event_id > cursor.last_digest_event_id:
        cursor.last_digest_event_id = event_id
    db.commit()


__all__ = [
    "TaskEventService",
    "emit_task_event",
    "get_task_events",
    "mark_task_events_consumed",
    "DECISION_EVENT_TYPES",
    "claim_event",
    "get_digest",
    "advance_cursor",
]
