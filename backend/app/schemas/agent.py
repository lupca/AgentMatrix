from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


AgentTypeValue = Literal["cli", "api"]
ProviderValue = Literal["anthropic", "google", "openai"]


class AgentCreate(BaseModel):
    id: str
    name: str
    role: str
    capabilities: list[Any] | None = None
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
    role: str | None = None
    capabilities: list[Any] | None = None
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
    capabilities: list[Any] | None = Field(default_factory=list)
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


class AgentSuggestion(BaseModel):
    agent_id: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str
