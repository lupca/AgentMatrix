from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict

class SessionCreate(BaseModel):
    task_id: str | None = None
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = None

class SessionUpdate(BaseModel):
    task_id: str | None = None
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = None

class Session(BaseModel):
    id: str
    task_id: str | None = None
    thread_id: str | None = None
    current_gate: str | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    messages: list[dict[str, Any]] | None = []
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
