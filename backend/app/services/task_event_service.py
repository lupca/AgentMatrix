"""TaskEventService for managing task state change events (CTV2-114)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import TaskEvent


class _EmitDescriptor:
    """Descriptor enabling TaskEventService.emit to work on both class and instance."""

    def __get__(self, instance: TaskEventService | None, owner: type[TaskEventService]):
        if instance is not None:
            def instance_emit(
                task_id: str,
                event_type: str,
                payload: dict[str, Any] | None = None,
                db: Session | None = None,
            ) -> TaskEvent:
                return emit_task_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload=payload,
                    db=db or instance.db,
                )

            return instance_emit
        else:
            def class_emit(
                task_id: str,
                event_type: str,
                payload: dict[str, Any] | None = None,
                db: Session | None = None,
            ) -> TaskEvent:
                return emit_task_event(
                    task_id=task_id,
                    event_type=event_type,
                    payload=payload,
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


def emit_task_event(
    task_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    db: Session | None = None,
) -> TaskEvent:
    """Record a new task event in the database."""
    if payload is None:
        payload = {}

    own_session = False
    if db is None:
        from app.db.base import SessionLocal

        db = SessionLocal()
        own_session = True

    try:
        event = TaskEvent(
            task_id=task_id,
            event_type=event_type,
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


__all__ = [
    "TaskEventService",
    "emit_task_event",
    "get_task_events",
    "mark_task_events_consumed",
]
