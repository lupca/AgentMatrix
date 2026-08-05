"""Notification service: select, format, claim, record (CTV2-1381).

Selects decision-grade TaskEvents that have no delivery row yet,
formats actionable Telegram messages, and records outcomes in
notification_deliveries.  All DB work happens in short transactions;
the HTTP call is always performed with no session open.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import NotificationDelivery, Task, TaskEvent

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LEN = 4096

# CTV2-1400: Telegram is a whitelist, not a blacklist -- adding a new event
# type here requires a spec change (e6ee1eb0), not just a code change.
# Decoupled from TaskEvent.kind ("decision" vs "info"), which drives an
# unrelated coordinator-side digest/claim mechanism (task_event_service.
# DECISION_EVENT_TYPES) with its own, wider membership.
TELEGRAM_EVENT_TYPES = frozenset({
    "human_question",
    "task_done",
    "cost_brake",
    "deadman",
})


@dataclass(frozen=True)
class DeliveryClaim:
    """Plain snapshot of a newly-inserted delivery row.

    Returned by claim()/mark_skipped() so callers can use the values after
    the DB session is closed without hitting DetachedInstanceError.
    """

    id: int
    correlation_token: str
    attempts: int
    task_event_id: int
    task_id: str
    status: str


def select_pending_events(db: Session, limit: int = 50) -> list[TaskEvent]:
    """Return TaskEvents eligible for notification but not yet delivered.

    Criteria:
    - event_type IN TELEGRAM_EVENT_TYPES
    - no matching notification_deliveries row
    - created_at within TELEGRAM_MAX_EVENT_AGE_SECONDS
    - ORDER BY id ASC, LIMIT 50
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.TELEGRAM_MAX_EVENT_AGE_SECONDS
    )
    already_claimed = db.query(NotificationDelivery.task_event_id).subquery()
    return (
        db.query(TaskEvent)
        .filter(
            TaskEvent.event_type.in_(TELEGRAM_EVENT_TYPES),
            TaskEvent.created_at >= cutoff,
            ~TaskEvent.id.in_(already_claimed.select()),
        )
        .order_by(TaskEvent.id)
        .limit(limit)
        .all()
    )


def select_stale_events(db: Session, limit: int = 50) -> list[TaskEvent]:
    """Return decision events older than max age that still have no delivery row."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.TELEGRAM_MAX_EVENT_AGE_SECONDS
    )
    already_claimed = db.query(NotificationDelivery.task_event_id).subquery()
    return (
        db.query(TaskEvent)
        .filter(
            TaskEvent.event_type.in_(TELEGRAM_EVENT_TYPES),
            TaskEvent.created_at < cutoff,
            ~TaskEvent.id.in_(already_claimed.select()),
        )
        .order_by(TaskEvent.id)
        .limit(limit)
        .all()
    )


def claim(db: Session, event: TaskEvent) -> DeliveryClaim | None:
    """INSERT a pending delivery row and COMMIT.

    Returns a DeliveryClaim snapshot, or None if another worker already
    claimed this event (IntegrityError on the unique task_event_id).
    """
    token = str(uuid.uuid4())
    row = NotificationDelivery(
        task_id=event.task_id,
        task_event_id=event.id,
        channel="telegram",
        chat_id=settings.TELEGRAM_CHAT_ID or None,
        correlation_token=token,
        status="pending",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return DeliveryClaim(
        id=row.id,
        correlation_token=token,
        attempts=0,
        task_event_id=event.id,
        task_id=event.task_id,
        status="pending",
    )


def mark_skipped(db: Session, event: TaskEvent) -> DeliveryClaim | None:
    """INSERT a skipped delivery row for stale events and COMMIT."""
    token = str(uuid.uuid4())
    row = NotificationDelivery(
        task_id=event.task_id,
        task_event_id=event.id,
        channel="telegram",
        chat_id=settings.TELEGRAM_CHAT_ID or None,
        correlation_token=token,
        status="skipped",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return DeliveryClaim(
        id=row.id,
        correlation_token=token,
        attempts=0,
        task_event_id=event.id,
        task_id=event.task_id,
        status="skipped",
    )


def format_message(task: Task | None, event: TaskEvent, token: str) -> str:
    """Build the Telegram message body for a whitelisted event.

    Contains: task id, task title, event type, event-specific fields,
    and the correlation token.  Truncated to <= 4096 chars.

    ``task`` is ``None`` for ``human_question`` events not tied to any task
    (``ask_human`` was called with no ``task_id``).
    """
    lines = [f"🔔 {event.event_type}"]
    if task is not None:
        lines.append(f"Task: {task.id} — {task.title}")
    payload: dict[str, Any] = event.payload or {}

    if event.event_type == "gate_pending":
        gate = payload.get("gate", "?")
        gate_record_id = payload.get("gate_record_id", "?")
        lines.append(f"Gate: {gate} (record #{gate_record_id})")
        lines.append("Action required: review and approve/reject.")
    elif event.event_type == "run_failed":
        run_id = payload.get("run_id", "?")
        error = str(payload.get("error", "unknown"))
        if len(error) > 200:
            error = error[:197] + "..."
        lines.append(f"Run: {run_id}")
        lines.append(f"Error: {error}")
    elif event.event_type == "escalated":
        reason = payload.get("reason", "unknown")
        lines.append(f"Reason: {reason}")
    elif event.event_type == "human_question":
        lines.append(f"Question: {payload.get('question', '?')}")
        lines.append(f"Why human: {payload.get('why_human', '?')}")
        options = payload.get("options") or []
        if options:
            lines.append(f"Options: {', '.join(str(o) for o in options)}")
        lines.append("Answer in the coordinator chat -- this tool cannot receive a reply.")
    elif event.event_type == "task_done":
        lines.append(f"Executor: {payload.get('executor', '?')}")
        lines.append(f"Commit: {payload.get('commit') or '(no commit)'}")
    elif event.event_type == "cost_brake":
        lines.append(f"Cost: ${payload.get('cost_usd', '?')} >= ${payload.get('max_cost_usd_per_task', '?')}")
        lines.append("Autonomy stopped this task -- spending needs a human decision.")
    elif event.event_type == "deadman":
        lines.append(f"No progress for {payload.get('no_progress_minutes', '?')} min.")
        lines.append(str(payload.get("reason", "")))

    lines.append(f"\n🔑 {token}")
    text = "\n".join(lines)
    if len(text) > _MAX_MESSAGE_LEN:
        text = text[: _MAX_MESSAGE_LEN - 1] + "…"
    return text


def select_retryable_deliveries(db: Session, limit: int = 50) -> list[NotificationDelivery]:
    """Return failed deliveries with attempts < TELEGRAM_MAX_ATTEMPTS."""
    return (
        db.query(NotificationDelivery)
        .filter(
            NotificationDelivery.status == "failed",
            NotificationDelivery.attempts < settings.TELEGRAM_MAX_ATTEMPTS,
        )
        .order_by(NotificationDelivery.id)
        .limit(limit)
        .all()
    )


def record_outcome(
    delivery_id: int,
    *,
    status: str,
    provider_message_id: str | None = None,
    last_error: str | None = None,
    sent_at: datetime | None = None,
    attempts: int = 1,
) -> None:
    """Write the send outcome in a fresh short session.

    Called AFTER the HTTP call, with no prior session open.
    """
    db = SessionLocal()
    try:
        row = db.query(NotificationDelivery).filter_by(id=delivery_id).first()
        if row is None:
            logger.warning("notification_delivery %s not found for outcome", delivery_id)
            return
        row.status = status
        row.attempts = attempts
        row.last_error = last_error
        row.provider_message_id = provider_message_id
        if sent_at is not None:
            row.sent_at = sent_at
        db.commit()
    except Exception:
        logger.exception("notification_delivery: failed to record outcome for %s", delivery_id)
        db.rollback()
    finally:
        db.close()
