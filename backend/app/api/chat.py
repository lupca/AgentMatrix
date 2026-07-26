import json
import uuid
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession
from app.db.base import get_db
from app.db.models import Task as TaskModel, Session as SessionModel
from app.services.llm import llm
from app.services.command_router import CommandRouter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    thread_id: str
    message: str


def get_or_create_session(thread_id: str, db: DBSession) -> SessionModel:
    # 1. Try finding session by ID or thread_id
    db_session = db.query(SessionModel).filter(
        (SessionModel.id == thread_id) | (SessionModel.thread_id == thread_id)
    ).first()

    if db_session:
        return db_session

    # 2. Try finding task by ID or session_id
    db_task = db.query(TaskModel).filter(
        (TaskModel.id == thread_id) | (TaskModel.session_id == thread_id)
    ).first()

    if db_task:
        if not db_task.session_id:
            db_task.session_id = str(uuid.uuid4())
            db.commit()
            db.refresh(db_task)

        db_session = db.query(SessionModel).filter(SessionModel.task_id == db_task.id).first()
        if not db_session:
            db_session = SessionModel(
                id=db_task.session_id,
                task_id=db_task.id,
                thread_id=db_task.session_id,
                messages=[]
            )
            db.add(db_session)
            db.commit()
            db.refresh(db_session)
        return db_session

    # 3. Fallback: Create standalone session
    db_session = SessionModel(
        id=thread_id,
        thread_id=thread_id,
        messages=[]
    )
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    return db_session


@router.post("/chat")
async def chat_endpoint(req: ChatRequest, db: DBSession = Depends(get_db)):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    db_session = get_or_create_session(req.thread_id, db)

    user_msg_id = f"msg-{uuid.uuid4()}"
    now_iso = datetime.now(timezone.utc).isoformat()
    user_msg = {
        "id": user_msg_id,
        "role": "user",
        "content": req.message,
        "timestamp": now_iso
    }

    current_messages = list(db_session.messages or [])
    current_messages.append(user_msg)
    db_session.messages = current_messages
    db.commit()

    command_router = CommandRouter(db)
    cmd, args = command_router.parse(req.message)

    assistant_msg_id = f"msg-{uuid.uuid4()}"

    if cmd:
        async def stream_command_response():
            start_payload = json.dumps({"type": "start", "id": assistant_msg_id})
            yield f"data: {start_payload}\n\n"

            cmd_result = await command_router.execute(cmd, args, db_session.id)
            full_content = json.dumps(cmd_result)

            chunk_payload = json.dumps({"type": "chunk", "content": full_content})
            yield f"data: {chunk_payload}\n\n"

            end_iso = datetime.now(timezone.utc).isoformat()
            assistant_msg = {
                "id": assistant_msg_id,
                "role": "assistant",
                "content": full_content,
                "timestamp": end_iso
            }

            try:
                db_session.messages = list(db_session.messages or []) + [assistant_msg]
                db.commit()
            except Exception as db_err:
                logger.error("Failed to persist assistant message: %s", db_err)

            done_payload = json.dumps({"type": "done", "id": assistant_msg_id, "content": full_content})
            yield f"data: {done_payload}\n\n"

        return StreamingResponse(stream_command_response(), media_type="text/event-stream")

    llm_messages = []
    if db_session.task_id:
        task = db.query(TaskModel).filter(TaskModel.id == db_session.task_id).first()
        if task:
            sys_content = f"You are Control Tower AI Assistant helping with Task [{task.id}]: '{task.title}'. Project: '{task.project}', Status: '{task.status}'."
            if task.plan:
                sys_content += f"\nTask Plan:\n{task.plan}"
            llm_messages.append({"role": "system", "content": sys_content})

    for msg in current_messages:
        llm_messages.append({"role": msg["role"], "content": msg["content"]})

    async def stream_response():
        full_content = ""
        start_payload = json.dumps({"type": "start", "id": assistant_msg_id})
        yield f"data: {start_payload}\n\n"

        try:
            async for chunk in llm.stream_async(
                llm_messages,
                operation="chat",
                session_id=db_session.id,
                task_id=db_session.task_id,
                db_session=db,
            ):
                full_content += chunk
                chunk_payload = json.dumps({"type": "chunk", "content": chunk})
                yield f"data: {chunk_payload}\n\n"
        except Exception as e:
            logger.error("Error streaming from LLM: %s", e)
            err_payload = json.dumps({"type": "error", "content": str(e)})
            yield f"data: {err_payload}\n\n"

        end_iso = datetime.now(timezone.utc).isoformat()
        assistant_msg = {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": full_content,
            "timestamp": end_iso
        }

        try:
            db_session.messages = list(db_session.messages or []) + [assistant_msg]
            db.commit()
        except Exception as db_err:
            logger.error("Failed to persist assistant message: %s", db_err)

        done_payload = json.dumps({"type": "done", "id": assistant_msg_id, "content": full_content})
        yield f"data: {done_payload}\n\n"

    return StreamingResponse(stream_response(), media_type="text/event-stream")
