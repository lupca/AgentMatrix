from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


AgentTypeValue = Literal["cli", "api"]
ProviderValue = Literal["anthropic", "google", "openai"]
AgentRoleValue = Literal["executor", "reviewer", "coordinator", "spec_plan"]


class AgentCreate(BaseModel):
    id: str
    name: str
    role: AgentRoleValue | None = None
    roles: list[AgentRoleValue] | None = None
    capabilities: list[str] | None = None
    status: str | None = "idle"
    type: str | None = None
    model: str | None = None
    effort: str | None = None
    cli: str | None = None
    agent_type: AgentTypeValue = "cli"
    api_key: str | None = None
    provider: ProviderValue | None = None
    base_url: str | None = None
    is_default: bool = False

    @model_validator(mode="before")
    @classmethod
    def validate_type_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        agent_type = values.get("agent_type", "cli")
        if agent_type == "api":
            if not values.get("api_key") or not str(values["api_key"]).strip():
                raise ValueError("API agents require an api_key")
            if not values.get("provider"):
                raise ValueError("API agents require a provider")
        elif "agent_type" in values and not values.get("cli"):
            raise ValueError("CLI agents require a cli tool")
        return values


class AgentUpdate(BaseModel):
    name: str | None = None
    role: AgentRoleValue | None = None
    roles: list[AgentRoleValue] | None = None
    capabilities: list[str] | None = None
    status: str | None = None
    type: str | None = None
    model: str | None = None
    effort: str | None = None
    cli: str | None = None
    agent_type: AgentTypeValue | None = None
    api_key: str | None = None
    provider: ProviderValue | None = None
    base_url: str | None = None
    is_default: bool | None = None


class Agent(BaseModel):
    id: str
    name: str
    role: str
    roles: list[AgentRoleValue] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    status: str
    type: str | None = None
    model: str | None = None
    effort: str | None = None
    cli: str | None = None
    agent_type: AgentTypeValue = "cli"
    provider: ProviderValue | None = None
    base_url: str | None = None
    has_api_key: bool = False
    is_default: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    archived_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="before")
    @classmethod
    def serialize_normalized_links(cls, value: Any) -> Any:
        if hasattr(value, "normalized_roles"):
            return {
                "id": value.id,
                "name": value.name,
                "role": value.role,
                "roles": value.normalized_roles,
                "capabilities": value.normalized_capabilities,
                "status": value.status,
                "type": value.type,
                "model": value.model,
                "effort": value.effort,
                "cli": value.cli,
                "agent_type": value.agent_type,
                "provider": value.provider,
                "base_url": value.base_url,
                "has_api_key": value.has_api_key,
                "is_default": value.is_default,
                "created_at": value.created_at,
                "updated_at": value.updated_at,
                "archived_at": value.archived_at,
            }
        return value


class AgentSuggestion(BaseModel):
    agent_id: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str
