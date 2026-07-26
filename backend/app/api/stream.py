"""SSE output replay and live streaming for agent runs."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import AgentOutputChunk, AgentRun
from app.workers.output_streamer import REDIS_URL, get_channel

router = APIRouter(prefix="/api", tags=["stream"])

TERMINAL_STATUSES = {"success", "failed", "timeout", "cancelled"}
HEARTBEAT_SECONDS = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))


async def create_redis_client():
    return aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        health_check_interval=30,
    )


def _sse(payload: dict[str, Any], *, event_id: int | None = None) -> str:
    parts: list[str] = []
    if event_id is not None:
        parts.append(f"id: {event_id}")
    event_type = payload.get("type")
    if event_type:
        parts.append(f"event: {event_type}")
    parts.append(
        f"data: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}"
    )
    return "\n".join(parts) + "\n\n"


def _status_payload(run: AgentRun) -> dict[str, Any]:
    return {
        "type": "status",
        "status": run.status,
        "exit_code": run.exit_code,
        "result_ref": run.result_ref,
        "error": run.error_message,
        "attempt": run.attempt,
    }


def _last_seen(
    header_value: str | None,
    query_value: int | None,
) -> int:
    if query_value is not None:
        return max(query_value, 0)
    try:
        return max(int(header_value or 0), 0)
    except ValueError:
        return 0


@router.get("/runs/{run_id}/stream")
async def stream_run_output(
    run_id: str,
    db: Session = Depends(get_db),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    resume_after = _last_seen(last_event_id_header, last_event_id)

    async def event_generator() -> AsyncGenerator[str, None]:
        redis_client = None
        pubsub = None
        last_sent = resume_after

        try:
            # Subscribe before reading history. Sequence indexes let us discard
            # Pub/Sub messages already represented by the database snapshot.
            if run.status not in TERMINAL_STATUSES:
                redis_client = await create_redis_client()
                pubsub = await _maybe_await(redis_client.pubsub())
                await pubsub.subscribe(get_channel(run_id))

            chunks = (
                db.query(AgentOutputChunk)
                .filter(AgentOutputChunk.run_id == run_id)
                .order_by(AgentOutputChunk.chunk_index)
                .all()
            )
            history_index = 0
            for chunk in chunks:
                for line in chunk.content.split("\n"):
                    history_index += 1
                    if history_index <= resume_after:
                        continue
                    last_sent = history_index
                    yield _sse(
                        {
                            "type": "history",
                            "content": line,
                            "index": history_index,
                        },
                        event_id=history_index,
                    )

            db.expire_all()
            current_run = (
                db.query(AgentRun)
                .filter(AgentRun.id == run_id)
                .populate_existing()
                .first()
            )
            if current_run is None:
                return
            if current_run.status in TERMINAL_STATUSES:
                yield _sse(_status_payload(current_run))
                yield _sse({"type": "done"})
                return

            while True:
                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=HEARTBEAT_SECONDS,
                        ),
                        timeout=HEARTBEAT_SECONDS + 1,
                    )
                except asyncio.TimeoutError:
                    message = None

                if message is None:
                    db.expire_all()
                    current_run = (
                        db.query(AgentRun)
                        .filter(AgentRun.id == run_id)
                        .populate_existing()
                        .first()
                    )
                    if current_run and current_run.status in TERMINAL_STATUSES:
                        yield _sse(_status_payload(current_run))
                        yield _sse({"type": "done"})
                        return
                    yield ": heartbeat\n\n"
                    continue

                data = message.get("data") if isinstance(message, dict) else message
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    payload = json.loads(data)
                except (TypeError, json.JSONDecodeError):
                    continue

                event_index = payload.get("index")
                if isinstance(event_index, int):
                    if event_index <= last_sent:
                        continue
                    last_sent = event_index
                yield _sse(payload, event_id=event_index)

                if (
                    payload.get("type") == "status"
                    and payload.get("status") in TERMINAL_STATUSES
                ):
                    yield _sse({"type": "done"})
                    return
        except asyncio.CancelledError:
            raise
        except (aioredis.ConnectionError, aioredis.TimeoutError) as exc:
            yield _sse(
                {
                    "type": "error",
                    "error": "stream_unavailable",
                    "message": str(exc),
                    "retry": True,
                }
            )
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(get_channel(run_id))
                finally:
                    await _close(pubsub)
            if redis_client is not None:
                await _close(redis_client)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _close(resource: Any) -> None:
    close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if close is not None:
        await _maybe_await(close())


@router.get("/runs/{run_id}/output")
def get_run_output(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.query(AgentRun).filter(AgentRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    chunks = (
        db.query(AgentOutputChunk)
        .filter(AgentOutputChunk.run_id == run_id)
        .order_by(AgentOutputChunk.chunk_index)
        .all()
    )
    return {
        "run_id": run_id,
        "status": run.status,
        "output": "\n".join(chunk.content for chunk in chunks),
        "line_count": run.output_lines,
        "byte_count": run.output_bytes,
    }
