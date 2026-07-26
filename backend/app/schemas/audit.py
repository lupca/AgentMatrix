from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict

class AuditLogCreate(BaseModel):
    task_id: str | None = None
    action: str
    actor: str | None = None
    details: dict[str, Any] | None = None

class AuditLog(BaseModel):
    id: int
    task_id: str | None = None
    action: str
    actor: str | None = None
    details: dict[str, Any] | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
