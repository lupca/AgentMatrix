"""Dramatiq broker configuration.

RedisBroker keeps unacknowledged messages until a worker acknowledges them.  With
Redis AOF enabled in docker-compose this provides queue and worker-restart
recovery.
"""

import os

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage, Middleware

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

redis_broker = RedisBroker(
    url=REDIS_URL,
    namespace="control-tower",
    heartbeat_timeout=60_000,
)
redis_broker.add_middleware(CurrentMessage())


class _OutboxPollerBootstrap(Middleware):
    """Kick off the outbox_publisher/reconcile self-rescheduling poll loops
    (CTV2-205) once per worker process boot -- see
    `app.workers.outbox_publisher`. Imported lazily to avoid this package's
    module-load-time import being part of the broker's import cycle.
    """

    def after_worker_boot(self, broker, worker):
        from app.workers.outbox_publisher import (
            outbox_publisher,
            reconcile_orphaned_agent_runs,
        )
        from app.workers.notification_dispatcher import (
            notification_dispatcher,
        )
        from app.workers.deadman_monitor import deadman_monitor

        outbox_publisher.send()
        reconcile_orphaned_agent_runs.send()
        notification_dispatcher.send()
        deadman_monitor.send()


redis_broker.add_middleware(_OutboxPollerBootstrap())
dramatiq.set_broker(redis_broker)
