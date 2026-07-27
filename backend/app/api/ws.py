import asyncio
import json
import logging
from typing import Set
from contextlib import asynccontextmanager
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["websocket"])

# Background task for Redis subscription
_redis_subscriber_task: asyncio.Task | None = None


class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)

    async def broadcast(self, message: dict):
        dead_connections = set()
        data = json.dumps(message)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(data)
            except Exception:
                dead_connections.add(connection)
        for dead in dead_connections:
            self.active_connections.discard(dead)


ws_manager = ConnectionManager()


async def _redis_subscriber():
    """Subscribe to Redis task events and broadcast to WebSocket clients."""
    from app.workers.output_streamer import redis_client, TASK_EVENTS_CHANNEL

    pubsub = redis_client.pubsub()
    try:
        pubsub.subscribe(TASK_EVENTS_CHANNEL)
        logger.info("Subscribed to Redis channel: %s", TASK_EVENTS_CHANNEL)

        while True:
            try:
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                        await ws_manager.broadcast(data)
                    except json.JSONDecodeError:
                        logger.warning("Invalid JSON in Redis message")
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning("Redis subscriber error: %s", e)
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        logger.info("Redis subscriber shutting down")
    finally:
        pubsub.unsubscribe()
        pubsub.close()


def start_redis_subscriber():
    """Start the Redis subscriber background task."""
    global _redis_subscriber_task
    if _redis_subscriber_task is None or _redis_subscriber_task.done():
        _redis_subscriber_task = asyncio.create_task(_redis_subscriber())
        logger.info("Started Redis subscriber task")


def stop_redis_subscriber():
    """Stop the Redis subscriber background task."""
    global _redis_subscriber_task
    if _redis_subscriber_task and not _redis_subscriber_task.done():
        _redis_subscriber_task.cancel()
        logger.info("Stopped Redis subscriber task")


def publish_event(message: dict) -> None:
    """Fan out an application event to WebSocket clients when available.

    Gate transitions are performed by both the async API process and the
    synchronous worker.  The latter has no event loop to await, so delivery
    is deliberately best-effort here; the durable session message remains
    the source of truth for reconnecting clients.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(ws_manager.broadcast(message))


@router.websocket("")
@router.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                if payload.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.warning("WebSocket connection error: %s", e)
        ws_manager.disconnect(websocket)
