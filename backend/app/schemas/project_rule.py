"""Pydantic schemas for ProjectRule entity."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ProjectRuleBase(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=500)
    globs: list[str] = Field(default_factory=list)
    content: str
    priority: int = 0


class ProjectRuleCreate(ProjectRuleBase):
    pass


class ProjectRuleUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    globs: list[str] | None = None
    content: str | None = None
    priority: int | None = None


class ProjectRuleRead(ProjectRuleBase):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
