from pydantic import BaseModel, ConfigDict, Field


class OverviewStats(BaseModel):
    total_tasks: int
    done_tasks: int
    active_tasks: int
    by_status: dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class ProjectStats(BaseModel):
    project_id: str
    project_name: str | None = None
    total_tasks: int
    done_tasks: int
    active_tasks: int
    by_status: dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class AgentStats(BaseModel):
    agent_id: str
    name: str | None = None
    role: str | None = None
    tasks_executed: int = 0
    tasks_reviewed: int = 0
    tasks_completed: int = 0
    success_rate: float = 1.0
    active_tasks: int = 0

    model_config = ConfigDict(from_attributes=True)
