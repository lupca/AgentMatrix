import json
import uuid
import logging
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession
from app.db.base import get_db
from app.db.models import Session as SessionModel
from app.graph.context import invalidate_context_snapshot
from app.services.command_router import CommandRouter
from app.services.coordinator import CoordinatorService
from app.services.tool_registry import dump_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    thread_id: str
    message: str
    model: str | None = None
    provider: str | None = None
    idempotency_key: str | None = None


def get_or_create_session(thread_id: str, db: DBSession) -> SessionModel:
    """Backward-compatible helper used by older API tests and callers."""

    return CoordinatorService(db).get_or_create_session(thread_id)


@router.get("/tools")
async def list_tools():
    """Registry dump (name, description, slash_alias, tier, group) for the
    chat UI tool palette and ``/help``."""

    return {"tools": dump_registry()}


@router.post("/chat")
async def chat_endpoint(
    req: ChatRequest,
    db: DBSession = Depends(get_db),
    idempotency_header: str | None = Header(None, alias="Idempotency-Key"),
):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    coordinator = CoordinatorService(db)
    db_session = coordinator.get_or_create_session(req.thread_id)
    turn_id = req.idempotency_key or idempotency_header or str(uuid.uuid4())

    command_router = CommandRouter(db)
    cmd, args = command_router.parse(req.message)

    assistant_msg_id = f"msg-{uuid.uuid4()}"

    if cmd:
        coordinator.ensure_user_message(db_session, req.message, turn_id)
        completed_command = coordinator.completed_turn(db_session, turn_id)

        async def stream_command_response():
            start_payload = json.dumps({"type": "start", "id": assistant_msg_id})
            yield f"data: {start_payload}\n\n"

            if completed_command is not None:
                full_content = str(completed_command.get("content", ""))
            else:
                cmd_result = await command_router.execute(cmd, args, db_session.id)
                # Slash commands include task mutations (create, dispatch,
                # verdict, cancellation).  Invalidate the local snapshot so
                # the following user-chat turn sees committed state.
                invalidate_context_snapshot(db, project_id=db_session.project_id)
                full_content = json.dumps(cmd_result)

            chunk_payload = json.dumps({"type": "chunk", "content": full_content})
            yield f"data: {chunk_payload}\n\n"

            if completed_command is None:
                try:
                    coordinator.append_message(
                        db_session,
                        role="assistant",
                        content=full_content,
                        message_id=assistant_msg_id,
                        turn_id=turn_id,
                        idempotency_key=turn_id,
                        status="complete",
                    )
                except Exception as db_err:
                    logger.error("Failed to persist assistant message: %s", db_err)

            done_payload = json.dumps({"type": "done", "id": assistant_msg_id, "content": full_content})
            yield f"data: {done_payload}\n\n"

        return StreamingResponse(stream_command_response(), media_type="text/event-stream")

    try:
        coordinator.validate_selection(
            db_session,
            model=req.model,
            provider=req.provider,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def stream_response():
        full_content = ""
        start_payload = json.dumps({"type": "start", "id": assistant_msg_id})
        yield f"data: {start_payload}\n\n"

        try:
            async for chunk in coordinator.stream_turn(
                db_session,
                req.message,
                model=req.model,
                provider=req.provider,
                idempotency_key=turn_id,
            ):
                if isinstance(chunk, dict):
                    chunk_payload = json.dumps(chunk, ensure_ascii=False, default=str)
                else:
                    full_content += chunk
                    chunk_payload = json.dumps({"type": "chunk", "content": chunk})
                yield f"data: {chunk_payload}\n\n"
        except Exception as e:
            logger.error("Error streaming from LLM: %s", e)
            err_payload = json.dumps({"type": "error", "content": str(e)})
            yield f"data: {err_payload}\n\n"
            return

        done_payload = json.dumps({"type": "done", "id": assistant_msg_id, "content": full_content})
        yield f"data: {done_payload}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")
