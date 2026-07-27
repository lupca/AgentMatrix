"""Redis channels and event publishing shared by workers and APIs."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    health_check_interval=30,
    retry=Retry(ExponentialBackoff(cap=2.0, base=0.1), retries=5),
    retry_on_error=[
        redis.ConnectionError,
        redis.TimeoutError,
        redis.BusyLoadingError,
        ConnectionResetError,
    ],
)


def get_channel(run_id: str) -> str:
    return f"agent_run:{run_id}:output"


def get_cancel_channel(run_id: str) -> str:
    return f"agent_run:{run_id}:control"


def get_cancel_key(run_id: str) -> str:
    return f"agent_run:{run_id}:cancel"


def _publish(run_id: str, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            redis_client.publish(get_channel(run_id), encoded)
            return
        except (redis.ConnectionError, redis.TimeoutError, redis.BusyLoadingError) as exc:
            last_error = exc
            time.sleep(0.1 * (2**attempt))
    logger.warning("Unable to publish Redis event for run %s: %s", run_id, last_error)


def publish_line(
    run_id: str,
    line: str,
    line_type: str = "stdout",
    *,
    line_index: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "type": line_type,
        "content": line,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if line_index is not None:
        payload["index"] = line_index
    _publish(run_id, payload)


def publish_status(run_id: str, status: str, **kwargs: Any) -> None:
    _publish(
        run_id,
        {
            "type": "status",
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        },
    )


def request_cancel(run_id: str, *, ttl_seconds: int = 86_400) -> None:
    """Use both a durable key and Pub/Sub control notification."""
    with redis_client.pipeline(transaction=False) as pipe:
        pipe.set(get_cancel_key(run_id), "1", ex=ttl_seconds)
        pipe.publish(
            get_cancel_channel(run_id),
            json.dumps({"action": "cancel", "run_id": run_id}),
        )
        pipe.execute()


def clear_cancel_request(run_id: str) -> None:
    try:
        redis_client.delete(get_cancel_key(run_id))
    except (redis.ConnectionError, redis.TimeoutError, redis.BusyLoadingError):
        logger.warning("Could not clear cancellation key for run %s", run_id)


def is_cancel_requested(run_id: str) -> bool:
    try:
        return bool(redis_client.exists(get_cancel_key(run_id)))
    except (redis.ConnectionError, redis.TimeoutError, redis.BusyLoadingError):
        return False
