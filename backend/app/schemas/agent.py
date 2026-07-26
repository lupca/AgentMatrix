from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class AgentCreate(BaseModel):
    id: str
    name: str
    role: str
    capabilities: list[Any] | None = None
    status: str | None = "idle"


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    capabilities: list[Any] | None = None
    status: str | None = None


class Agent(BaseModel):
    id: str
    name: str
    role: str
    capabilities: list[Any] | None = Field(default_factory=list)
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AgentSuggestion(BaseModel):
    agent_id: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str
