"""Self-rescheduling Dramatiq actor for the deadman monitor (CTV2-1400).

Mirrors notification_dispatcher: short-lived sessions, self-reschedule in
``finally``. The actual "who's stalled, did we already warn" logic lives in
``app.services.deadman_service`` so it can be tested without Dramatiq.
"""

from __future__ import annotations

import logging
import os

import dramatiq

from app.db.base import SessionLocal
from app.services.deadman_service import fire_deadman_events
from app.workers import redis_broker

logger = logging.getLogger(__name__)

DEADMAN_POLL_INTERVAL_MS = max(
    10_000, int(os.getenv("DEADMAN_POLL_INTERVAL_MS", "60000"))
)


def poll_tick() -> int:
    """One poll iteration: fire deadman events for newly-stalled tasks."""
    db = SessionLocal()
    try:
        events = fire_deadman_events(db)
        return len(events)
    finally:
        db.close()


@dramatiq.actor(broker=redis_broker, max_retries=0, time_limit=30_000)
def deadman_monitor() -> dict[str, int]:
    try:
        fired = poll_tick()
        if fired:
            logger.info("deadman_monitor: fired %s event(s)", fired)
        return {"fired": fired}
    except Exception:
        logger.exception("deadman_monitor: poll failed")
        return {"fired": 0}
    finally:
        deadman_monitor.send_with_options(delay=DEADMAN_POLL_INTERVAL_MS)
