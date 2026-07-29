"""Background poll loop for the transactional outbox (CTV2-205).

`outbox_publisher` and `reconcile_orphaned_agent_runs` are self-rescheduling
Dramatiq actors rather than actors triggered by application events: each one
re-sends itself with a delay in a `finally` block, so once the loop is
kicked off (see `app.workers._OutboxPollerBootstrap.after_worker_boot`) it
keeps running for the lifetime of the worker process, independent of
request traffic. Both are idempotent no-ops when there is nothing to do, so
running them from more than one worker process concurrently is safe.
"""

from __future__ import annotations

import logging
import os

import dramatiq

from app.db.base import SessionLocal
from app.services.outbox import publish_pending_events, reconcile_orphaned_runs
from app.workers import redis_broker

logger = logging.getLogger(__name__)

OUTBOX_POLL_INTERVAL_MS = max(1_000, int(os.getenv("OUTBOX_POLL_INTERVAL_MS", "5000")))
RECONCILE_POLL_INTERVAL_MS = max(
    1_000, int(os.getenv("OUTBOX_RECONCILE_INTERVAL_MS", "60000"))
)


@dramatiq.actor(broker=redis_broker, max_retries=0, time_limit=30_000)
def outbox_publisher() -> dict[str, int]:
    db = SessionLocal()
    try:
        counts = publish_pending_events(db)
        if counts["published"] or counts["dead_lettered"]:
            logger.info("outbox_publisher: %s", counts)
        return counts
    except Exception:
        logger.exception("outbox_publisher: poll failed")
        return {"published": 0, "deferred": 0, "dead_lettered": 0}
    finally:
        db.close()
        outbox_publisher.send_with_options(delay=OUTBOX_POLL_INTERVAL_MS)


@dramatiq.actor(broker=redis_broker, max_retries=0, time_limit=30_000)
def reconcile_orphaned_agent_runs() -> int:
    db = SessionLocal()
    try:
        count = reconcile_orphaned_runs(db)
        if count:
            logger.warning(
                "reconcile_orphaned_agent_runs: recovered %s orphaned run(s)", count
            )
        return count
    except Exception:
        logger.exception("reconcile_orphaned_agent_runs: poll failed")
        return 0
    finally:
        db.close()
        reconcile_orphaned_agent_runs.send_with_options(delay=RECONCILE_POLL_INTERVAL_MS)
