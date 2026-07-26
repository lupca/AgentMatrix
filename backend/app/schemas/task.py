from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict

class TaskCreate(BaseModel):
    id: str | None = None
    project: str
    title: str
    status: str | None = "todo"
    priority: str | None = None
    risk: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    acceptance_criteria: list[str] | None = None
    files: list[str] | None = None
    tests: list[str] | None = None
    flows: list[str] | None = None
    plan: str | None = None
    result_ref: str | None = None
    findings: list[str] | None = None
    verdict: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    deadline: date | None = None

class TaskUpdate(BaseModel):
    project: str | None = None
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    risk: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    acceptance_criteria: list[str] | None = None
    files: list[str] | None = None
    tests: list[str] | None = None
    flows: list[str] | None = None
    plan: str | None = None
    result_ref: str | None = None
    findings: list[str] | None = None
    verdict: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    deadline: date | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None

class Task(BaseModel):
    id: str
    project: str
    title: str
    status: str
    priority: str | None = None
    risk: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    acceptance_criteria: list[str] | None = []
    files: list[str] | None = []
    tests: list[str] | None = []
    flows: list[str] | None = []
    plan: str | None = None
    result_ref: str | None = None
    findings: list[str] | None = []
    verdict: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    deadline: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
