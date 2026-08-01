from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


REVIEW_RESULT_SCHEMA_VERSION = "1.0"
SPEC_PLAN_RESULT_SCHEMA_VERSION = "1.1"


class SpecPlanResult(BaseModel):
    """Versioned, machine-readable output produced by the spec/plan gate LLM call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[SPEC_PLAN_RESULT_SCHEMA_VERSION]
    acceptance_criteria: list[StrictStr] = Field(min_length=1)
    plan: StrictStr
    files: list[StrictStr] = Field(default_factory=list)
    tests: list[StrictStr] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"]
    spec_clarity: Literal["high", "medium", "low"]
    open_questions: list[StrictStr]


class ReviewACResult(BaseModel):
    """The reviewer's verdict and evidence for one acceptance criterion."""

    model_config = ConfigDict(extra="forbid", strict=True)

    criterion_id: StrictStr
    status: Literal["pass", "fail"]
    evidence: list[StrictStr]
    finding_ids: list[StrictStr]
    # Kept as optional compatibility metadata for older consumers.  The
    # criterion_id/status pair is the required v1 contract.
    ac_index: StrictInt | None = None
    ac_text: StrictStr | None = None
    # Legacy alias for status. The reviewer prompt has always asked for this
    # key, so with extra="forbid" it must be a real field, not a property —
    # otherwise every artifact that follows the prompt fails validation.
    verdict: Literal["pass", "fail"] | None = None

    @model_validator(mode="after")
    def _sync_verdict(self) -> "ReviewACResult":
        if self.verdict is None:
            self.verdict = self.status
        elif self.verdict != self.status:
            raise ValueError(
                f"verdict ({self.verdict}) contradicts status ({self.status})"
            )
        return self


class ReviewResult(BaseModel):
    """Versioned, machine-readable output produced by a code-review run."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[REVIEW_RESULT_SCHEMA_VERSION]
    task_id: StrictStr
    base: StrictStr
    head: StrictStr
    ac_results: list[ReviewACResult]
    findings: list["ReviewFinding"]
    tests_run: list[StrictStr]
    tests_passed: list[StrictStr]


class ReviewFinding(BaseModel):
    """A structured, location-specific finding from a reviewer."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: StrictStr
    severity: StrictStr
    category: StrictStr
    file: StrictStr
    line: StrictInt
    description: StrictStr


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
    spec_clarity: str | None = None
    open_questions: list[str] | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    workflow_state: Literal[
        "waiting_human", "executing", "reviewing", "blocked", "terminal"
    ] = "executing"

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
    depends_on: list[str] | None = None


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
    spec_clarity: str | None = None
    open_questions: list[str] | None = None
    executor: str | None = None
    reviewer: str | None = None
    acceptance_criteria: list[Any] | None = []
    files: list[Any] | None = []
    tests: list[Any] | None = []
    flows: list[Any] | None = []
    depends_on: list[str] | None = []
    plan: str | None = None
    result_ref: str | None = None
    landed_ref: str | None = None
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
    archived_at: datetime | None = None
    workflow_state: Literal[
        "waiting_human", "executing", "reviewing", "blocked", "terminal"
    ] = "executing"

    model_config = ConfigDict(from_attributes=True)
