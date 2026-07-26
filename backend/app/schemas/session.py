from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict
from app.db.models import ContextLevel, SessionStatus


class SessionCreate(BaseModel):
    context_level: ContextLevel = ContextLevel.GLOBAL
    project_id: str | None = None
    task_id: str | None = None
    title: str | None = None
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = None


class SessionUpdate(BaseModel):
    title: str | None = None
    status: SessionStatus | None = None
    pinned: bool | None = None
    task_id: str | None = None
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = None


class Session(BaseModel):
    id: str
    context_level: ContextLevel
    project_id: str | None = None
    task_id: str | None = None
    title: str | None = None
    status: SessionStatus
    pinned: bool = False
    message_count: int = 0
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = []
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_activity_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
