from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class GateRecordCreate(BaseModel):
    task_id: str
    gate_type: str
    status: str | None = "pending"
    actor: str = "system"
    mode: str = "supervised"
    idempotency_key: str
    input_hash: str
    output_ref: str | None = None
    parent_id: int | None = None
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
    actor: str
    mode: str
    idempotency_key: str
    input_hash: str
    output_ref: str | None = None
    parent_id: int | None = None
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
    tags: list[Any] = Field(default_factory=list)
    current_gate: str = "spec"
    status: str = "todo"
    mode: str = "supervised"
    acceptance_criteria: list[Any] = Field(default_factory=list)
    files: list[Any] = Field(default_factory=list)
    tests: list[Any] = Field(default_factory=list)
    plan: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    result_ref: str | None = None
    findings: list[Any] = Field(default_factory=list)
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
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    project: str
    title: str
    raw_input: str | None = ""
    tags: list[Any] | None = None
    mode: str | None = "supervised"
    priority: str | None = None
    risk: str | None = None
    acceptance_criteria: list[Any] | None = None
    files: list[Any] | None = None
    tests: list[Any] | None = None
    flows: list[Any] | None = None
    plan: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    session_id: str | None = None
    deadline: date | None = None


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    project: str | None = None
    title: str | None = None
    raw_input: str | None = None
    tags: list[Any] | None = None
    priority: str | None = None
    risk: str | None = None
    acceptance_criteria: list[Any] | None = None
    files: list[Any] | None = None
    tests: list[Any] | None = None
    flows: list[Any] | None = None
    plan: str | None = None
    findings: list[Any] | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    error: str | None = None
    deadline: date | None = None


class Task(BaseModel):
    id: str
    session_id: str | None = None
    project: str
    title: str
    raw_input: str | None = ""
    tags: list[Any] | None = []
    status: str
    current_gate: str | None = "spec"
    mode: str | None = "supervised"
    priority: str | None = None
    risk: str | None = None
    executor: str | None = None
    reviewer: str | None = None
    acceptance_criteria: list[Any] | None = []
    files: list[Any] | None = []
    tests: list[Any] | None = []
    flows: list[Any] | None = []
    plan: str | None = None
    result_ref: str | None = None
    findings: list[Any] | None = []
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
