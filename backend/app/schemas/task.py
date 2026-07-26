from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class GateRecordCreate(BaseModel):
    task_id: str
    gate_type: str
    status: str | None = "pending"
    executor: str | None = None
    reviewer: str | None = None
    input_payload: dict[str, Any] | list[Any] | None = None
    output_payload: dict[str, Any] | list[Any] | None = None
    error_message: str | None = None


class GateRecord(BaseModel):
    id: int
    task_id: str
    gate_type: str
    status: str
    executor: str | None = None
    reviewer: str | None = None
    input_payload: dict[str, Any] | list[Any] | None = None
    output_payload: dict[str, Any] | list[Any] | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TaskState(BaseModel):
    raw_input: str = ""
    task_id: str | None = None
    project: str | None = None
    title: str | None = None
    current_gate: str = "spec"
    status: str = "todo"
    mode: str = "supervised"
    acceptance_criteria: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    plan: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    result_ref: str | None = None
    findings: list[str] = Field(default_factory=list)
    verdict: str | None = None
    awaiting_approval: bool = False
    approval_prompt: str | None = None
    dispatched_at: str | None = None
    completed_at: str | None = None
    risk: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TaskCreate(BaseModel):
    id: str | None = None
    project: str
    title: str
    raw_input: str | None = ""
    status: str | None = "todo"
    current_gate: str | None = "spec"
    mode: str | None = "supervised"
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
    awaiting_approval: bool | None = False
    approval_prompt: str | None = None
    error: str | None = None
    session_id: str | None = None
    deadline: date | None = None


class TaskUpdate(BaseModel):
    session_id: str | None = None
    project: str | None = None
    title: str | None = None
    raw_input: str | None = None
    status: str | None = None
    current_gate: str | None = None
    mode: str | None = None
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
    awaiting_approval: bool | None = None
    approval_prompt: str | None = None
    error: str | None = None
    deadline: date | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None


class Task(BaseModel):
    id: str
    session_id: str | None = None
    project: str
    title: str
    raw_input: str | None = ""
    status: str
    current_gate: str | None = "spec"
    mode: str | None = "supervised"
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
    awaiting_approval: bool | None = False
    approval_prompt: str | None = None
    error: str | None = None
    deadline: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    dispatched_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
