"""Persist telemetry for token-saving tools (graph, ocr, review results).

Before this, the system was blind: a graph call that failed degraded to []
with a log line nobody reads, and a successful one left no trace at all —
no call counts, no success rate, no measure of how much context these
tools actually deliver. Every invocation now lands one row in
``tool_metrics`` (analyzable via query_db).

Recording must NEVER break the tool call it observes: it uses its own
short-lived session and swallows every failure.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def record_tool_metric(
    *,
    tool: str,
    source: str,
    ok: bool,
    duration_ms: int | None = None,
    result_count: int | None = None,
    bytes_out: int | None = None,
    cache_hit: bool = False,
    task_id: str | None = None,
    error: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    import os

    # The test suite (conftest sets TESTING=1) must never leak telemetry
    # rows into whatever real database DATABASE_URL happens to point at.
    if os.environ.get("TESTING") == "1":
        return
    try:
        from app.db.base import SessionLocal
        from app.db.models import ToolMetric

        db = SessionLocal()
        try:
            db.add(ToolMetric(
                tool=tool[:50],
                source=source[:30],
                task_id=(task_id or None),
                ok=ok,
                cache_hit=cache_hit,
                duration_ms=duration_ms,
                result_count=result_count,
                bytes_out=bytes_out,
                error=(error or None) and str(error)[:2000],
                payload=payload,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:  # observability must never take down the observed call
        logger.debug("tool metric recording failed for %s/%s", source, tool, exc_info=True)
