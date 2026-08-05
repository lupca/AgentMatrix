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


#: How long a boot-kick claim is held. Only needs to outlive the spread
#: between sibling processes booting, not the poll loops themselves.
_BOOT_CLAIM_TTL_MS = 30_000


class _OutboxPollerBootstrap(Middleware):
    """Kick off the self-rescheduling poll loops (CTV2-205) exactly once per
    worker BOOT -- not once per worker PROCESS. Imported lazily to avoid this
    package's module-load-time import being part of the broker's import cycle.

    dramatiq runs `--processes N`, so this hook fires N times, and each poll
    loop reschedules itself forever. Every extra kick therefore forks a
    permanent parallel copy of the loop, doubling every poll for the life of
    the worker. CTV2-1401 saw this as duplicate Telegram messages: deadman
    fired twice per stall, 30ms apart, because `--processes 2` had started two
    immortal deadman loops.

    A short-lived Redis claim makes the first process in win and the rest
    no-op. It is deliberately NOT a long lock: if the winner dies, dramatiq
    requeues its in-flight message, so loop survival does not depend on this.
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

        loops = {
            "outbox_publisher": outbox_publisher,
            "reconcile_orphaned_agent_runs": reconcile_orphaned_agent_runs,
            "notification_dispatcher": notification_dispatcher,
            "deadman_monitor": deadman_monitor,
        }
        client = getattr(broker, "client", None)
        for name, actor in loops.items():
            if client is not None:
                try:
                    claimed = client.set(
                        f"control-tower:boot-kick:{name}",
                        "1",
                        nx=True,
                        px=_BOOT_CLAIM_TTL_MS,
                    )
                except Exception:
                    # Never let a Redis hiccup stop the loops from starting;
                    # a duplicate loop is bad, no loop at all is worse.
                    claimed = True
                if not claimed:
                    continue
            actor.send()


redis_broker.add_middleware(_OutboxPollerBootstrap())
dramatiq.set_broker(redis_broker)
