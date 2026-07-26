"""Dramatiq broker configuration.

RedisBroker keeps unacknowledged messages until a worker acknowledges them.  With
Redis AOF enabled in docker-compose this provides queue and worker-restart
recovery.
"""

import os

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

redis_broker = RedisBroker(
    url=REDIS_URL,
    namespace="control-tower",
    heartbeat_timeout=60_000,
)
redis_broker.add_middleware(CurrentMessage())
dramatiq.set_broker(redis_broker)
