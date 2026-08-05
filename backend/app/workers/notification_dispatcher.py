"""Self-rescheduling Dramatiq actor for Telegram notifications (CTV2-1381).

Mirrors outbox_publisher: max_retries=0, reschedules in finally.
Per event: claim+commit+close → HTTP → new session for outcome.
No-op when disabled or credentials missing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import dramatiq

from app.core.config import settings
from app.db.base import SessionLocal
from app.db.models import Task, TaskEvent
from app.services.notification_service import (
    claim,
    format_message,
    mark_skipped,
    record_outcome,
    select_pending_events,
    select_retryable_deliveries,
    select_stale_events,
)
from app.services.providers.telegram import send_message as _tg_send
from app.workers import redis_broker

logger = logging.getLogger(__name__)

NOTIFY_POLL_INTERVAL_MS = max(
    1_000, int(os.getenv("NOTIFY_POLL_INTERVAL_MS", "5000"))
)


def _enabled() -> bool:
    return (
        settings.TELEGRAM_NOTIFY_ENABLED
        and bool(settings.TELEGRAM_BOT_TOKEN)
        and bool(settings.TELEGRAM_CHAT_ID)
    )


def _load_task(task_id: str) -> Task | None:
    db = SessionLocal()
    try:
        return db.query(Task).filter_by(id=task_id).first()
    finally:
        db.close()


def _load_event(event_id: int) -> TaskEvent | None:
    db = SessionLocal()
    try:
        return db.query(TaskEvent).filter_by(id=event_id).first()
    finally:
        db.close()


def _send_one(
    *,
    delivery_id: int,
    correlation_token: str,
    attempts: int,
    task: Task,
    event: TaskEvent,
    transport=None,
) -> None:
    """Send one Telegram message and record the outcome.

    Takes primitive delivery fields (not an ORM object) so it can be called
    after the claiming session is closed.  No DB session is open when the
    HTTP call fires.
    """
    text = format_message(task, event, correlation_token)
    ok, msg_id, error = _tg_send(
        bot_token=settings.TELEGRAM_BOT_TOKEN,
        chat_id=settings.TELEGRAM_CHAT_ID,
        text=text,
        timeout=settings.TELEGRAM_TIMEOUT_SECONDS,
        transport=transport,
    )
    new_attempts = attempts + 1
    if ok:
        record_outcome(
            delivery_id,
            status="sent",
            provider_message_id=msg_id,
            sent_at=datetime.now(timezone.utc),
            attempts=new_attempts,
        )
    else:
        record_outcome(
            delivery_id,
            status="failed",
            last_error=error,
            attempts=new_attempts,
        )


def _process_new_events(transport=None) -> int:
    """Claim and send for new undelivered events.  Returns count processed."""
    db = SessionLocal()
    try:
        stale = select_stale_events(db, limit=50)
        for ev in stale:
            mark_skipped(db, ev)

        events = select_pending_events(db, limit=50)
        # Snapshot what we need before closing the session.
        event_snapshots = [(e.id, e.task_id, e.event_type, e.payload) for e in events]
    finally:
        db.close()

    count = 0
    for event_id, task_id, event_type, payload in event_snapshots:
        db_claim = SessionLocal()
        try:
            # Re-fetch the event in this session for claim()
            ev = db_claim.query(TaskEvent).filter_by(id=event_id).first()
            if ev is None:
                continue
            delivery = claim(db_claim, ev)
            if delivery is not None:
                d_id, d_token, d_attempts = delivery.id, delivery.correlation_token, delivery.attempts
            else:
                d_id = None
        finally:
            db_claim.close()

        if d_id is None:
            continue

        task = _load_task(task_id)
        if task is None:
            record_outcome(
                d_id,
                status="failed",
                last_error="task not found",
                attempts=d_attempts + 1,
            )
            count += 1
            continue

        event = _load_event(event_id)
        if event is None:
            continue

        _send_one(
            delivery_id=d_id,
            correlation_token=d_token,
            attempts=d_attempts,
            task=task,
            event=event,
            transport=transport,
        )
        count += 1
    return count


def _process_retries(transport=None) -> int:
    """Retry failed deliveries under the attempt cap.  Returns count."""
    db = SessionLocal()
    try:
        retries = select_retryable_deliveries(db, limit=50)
        retry_snapshots = [
            (r.id, r.correlation_token, r.attempts, r.task_event_id, r.task_id)
            for r in retries
        ]
    finally:
        db.close()

    count = 0
    for d_id, d_token, d_attempts, event_id, task_id in retry_snapshots:
        event = _load_event(event_id)
        if event is None:
            continue
        task = _load_task(task_id)
        if task is None:
            continue

        _send_one(
            delivery_id=d_id,
            correlation_token=d_token,
            attempts=d_attempts,
            task=task,
            event=event,
            transport=transport,
        )
        count += 1
    return count


def poll_tick(transport=None) -> dict[str, int]:
    """One poll iteration: stale → retries → new.  Testable entry point.

    Retries run before new events so a delivery that just failed in the
    previous tick is not re-sent in the same tick it was created.
    """
    if not _enabled():
        return {"dispatched": 0, "skipped": 0}
    retry_count = _process_retries(transport=transport)
    new_count = _process_new_events(transport=transport)
    return {"dispatched": new_count + retry_count, "skipped": 0}


@dramatiq.actor(broker=redis_broker, max_retries=0, time_limit=30_000)
def notification_dispatcher() -> dict[str, int]:
    """Poll for undelivered decision events and send Telegram notifications."""
    try:
        result = poll_tick()
        if result["dispatched"]:
            logger.info("notification_dispatcher: %s", result)
        return result
    except Exception:
        logger.exception("notification_dispatcher: poll failed")
        return {"dispatched": 0, "skipped": 0}
    finally:
        notification_dispatcher.send_with_options(delay=NOTIFY_POLL_INTERVAL_MS)
