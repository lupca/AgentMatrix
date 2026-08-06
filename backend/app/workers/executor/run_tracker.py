"""Execution tracker for agent runs."""

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db.models import AgentRun, Task
from app.workers.output_streamer import is_cancel_requested

logger = logging.getLogger(__name__)


def _runner() -> Any:
    """Get agent_runner module dynamically so monkeypatched attributes are respected."""
    return sys.modules.get("app.workers.agent_runner")


def _get_attr(name: str, fallback: Any) -> Any:
    mod = _runner()
    return getattr(mod, name, fallback) if mod is not None else fallback


def _throttled_cancel_check(run_id: str, interval: float = 0.5) -> Callable[[], bool]:
    last_check = 0.0
    last_result = False

    def check() -> bool:
        nonlocal last_check, last_result
        check_fn = _get_attr("is_cancel_requested", is_cancel_requested)
        now = time.monotonic()
        if last_result or now - last_check >= interval:
            last_check = now
            last_result = check_fn(run_id)
        return last_result

    return check


class ExecutionTracker:
    """Tracks heartbeat and cancellation status for an agent run."""

    def __init__(
        self,
        db: Session,
        run_id: str,
        task_id: str,
        redis_cancel_check: Callable[[], bool] | None = None,
    ):
        self.db = db
        self.run_id = run_id
        self.task_id = task_id
        self._redis_cancel_check = (
            redis_cancel_check
            if redis_cancel_check is not None
            else _throttled_cancel_check(run_id)
        )

    def cancel_check(self) -> bool:
        if self._redis_cancel_check():
            return True
        current_run_status = (
            self.db.query(AgentRun.status)
            .filter(AgentRun.id == self.run_id)
            .scalar()
        )
        if current_run_status == "cancelled":
            return True
        current_task_status = (
            self.db.query(Task.status)
            .filter(Task.id == self.task_id)
            .scalar()
        )
        return current_task_status in {"done", "failed", "cancelled"}

    def record_heartbeat(self, pid: int | None = None) -> None:
        try:
            run = self.db.query(AgentRun).filter(AgentRun.id == self.run_id).first()
            if run is not None:
                run.updated_at = datetime.now(timezone.utc)
                self.db.commit()
        except Exception:
            self.db.rollback()
            logger.warning(
                "Failed to update heartbeat for run %s", self.run_id, exc_info=True
            )
