from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    consumed_at: datetime | None = None


class EventsPollResponse(BaseModel):
    events: list[TaskEventResponse]
    cursor: str
    has_more: bool
