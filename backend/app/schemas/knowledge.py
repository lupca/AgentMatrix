from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class KnowledgeItemCreate(BaseModel):
    id: str | None = None
    title: str
    category: str | None = "general"
    content: str | None = ""
    tags: list[Any] | None = Field(default_factory=list)
    project: str | None = None
    author: str | None = None


class KnowledgeItemUpdate(BaseModel):
    title: str | None = None
    category: str | None = None
    content: str | None = None
    tags: list[Any] | None = None
    project: str | None = None
    author: str | None = None
    status: str | None = None


class KnowledgeItem(BaseModel):
    id: str
    title: str
    category: str | None = "general"
    content: str | None = ""
    tags: list[Any] | None = Field(default_factory=list)
    project: str | None = None
    author: str | None = None
    status: str | None = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
