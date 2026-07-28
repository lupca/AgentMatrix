from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Any


class ProjectCreate(BaseModel):
    id: str
    name: str
    description: str | None = None
    context_md: str | None = None
    status: str | None = "active"
    repo_root: str | None = None
    task_prefix: str | None = None
    graph_status: str | None = "idle"
    next_task_seq: int = 0
    autonomy_policy: dict[str, Any] | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    context_md: str | None = None
    status: str | None = None
    repo_root: str | None = None
    task_prefix: str | None = None
    graph_status: str | None = None
    next_task_seq: int | None = None
    autonomy_policy: dict[str, Any] | None = None


class Project(BaseModel):
    id: str
    name: str
    description: str | None = None
    context_md: str | None = None
    status: str
    repo_root: str | None = None
    task_prefix: str | None = None
    graph_status: str | None = None
    next_task_seq: int | None = None
    autonomy_policy: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
