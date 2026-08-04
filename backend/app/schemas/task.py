from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)


REVIEW_RESULT_SCHEMA_VERSION = "1.0"
SPEC_PLAN_RESULT_SCHEMA_VERSION = "2.0"
PLAN_CRITIC_RESULT_SCHEMA_VERSION = "1.0"
NonEmptyStrictStr = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)
]


class PlanEvidence(BaseModel):
    """One reproducible fact used by the planner."""

    model_config = ConfigDict(extra="forbid", strict=True)

    fact: NonEmptyStrictStr
    source_type: Literal["command", "file", "query"]
    source: NonEmptyStrictStr
    result: NonEmptyStrictStr


class RuledOutApproach(BaseModel):
    """An alternative the planner considered and rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    approach: NonEmptyStrictStr
    reason: NonEmptyStrictStr


class PlanLimits(BaseModel):
    """Task-local ceilings enforced in addition to global safety brakes."""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_execution_rounds: StrictInt = Field(ge=1)
    max_tokens: StrictInt = Field(ge=1)
    max_cost_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0)


class SpecPlanResult(BaseModel):
    """Versioned, machine-readable output produced by the spec/plan gate LLM call."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[SPEC_PLAN_RESULT_SCHEMA_VERSION]
    acceptance_criteria: list[NonEmptyStrictStr]
    constraints: list[NonEmptyStrictStr]
    evidence: list[PlanEvidence] = Field(min_length=1)
    prior_art: list[NonEmptyStrictStr]
    ruled_out: list[RuledOutApproach]
    limits: PlanLimits | None
    plan: StrictStr
    files: list[StrictStr] = Field(default_factory=list)
    tests: list[StrictStr] = Field(default_factory=list)
    risk: Literal["low", "medium", "high"]
    spec_clarity: Literal["high", "medium", "low"]
    open_questions: list[StrictStr]

    @model_validator(mode="after")
    def _enforce_plan_contract(self) -> "SpecPlanResult":
        if not self.acceptance_criteria and not self.constraints:
            raise ValueError(
                "at least one acceptance criterion or constraint is required"
            )
        if self.risk == "high" and self.limits is None:
            raise ValueError("limits are required when risk is high")
        return self


class PlanCriticFinding(BaseModel):
    """A focused plan-critic finding backed by reproducible evidence."""

    model_config = ConfigDict(extra="forbid", strict=True)

    target: Literal[
        "evidence", "prior_art", "ruled_out", "constraints", "limits", "contract"
    ]
    description: NonEmptyStrictStr
    evidence: list[NonEmptyStrictStr] = Field(min_length=1)


class PlanCriticResult(BaseModel):
    """Independent, budgeted verdict on a generated plan."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[PLAN_CRITIC_RESULT_SCHEMA_VERSION]
    verdict: Literal["accept", "reject"]
    findings: list[PlanCriticFinding]
    summary: NonEmptyStrictStr

    @model_validator(mode="after")
    def _rejection_requires_findings(self) -> "PlanCriticResult":
        if self.verdict == "reject" and not self.findings:
            raise ValueError("a rejected plan requires at least one evidenced finding")
        return self


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
    # Legacy aliases used by some reviewer prompts
    legacy_ac_index: StrictInt | None = None
    legacy_ac_text: StrictStr | None = None
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
    # Optional toolchain metadata from automated checks (ruff, ocr, etc.)
    toolchain_results: dict[str, Any] | None = None


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
    constraints: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    prior_art: list[str] = Field(default_factory=list)
    ruled_out: list[dict[str, Any]] = Field(default_factory=list)
    limits: dict[str, Any] | None = None
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
    planner: str | None = None
    plan_critic: str | None = None
    plan_critic_status: str | None = None
    plan_critic_findings: list[dict[str, Any]] = Field(default_factory=list)
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
    constraints: list[str] | None = []
    evidence: list[dict[str, Any]] | None = []
    prior_art: list[str] | None = []
    ruled_out: list[dict[str, Any]] | None = []
    limits: dict[str, Any] | None = None
    planner: str | None = None
    plan_critic: str | None = None
    plan_critic_status: str | None = None
    plan_critic_findings: list[dict[str, Any]] | None = []
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
