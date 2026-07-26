from enum import Enum
from typing import Literal, Any
from pydantic import BaseModel, Field
from datetime import datetime


class GateType(str, Enum):
    SPEC = "spec"
    PLAN = "plan"
    DISPATCH = "dispatch"
    REVIEW_ORDER = "review_order"
    VERDICT = "verdict"


class Mode(str, Enum):
    PLAN_ONLY = "plan-only"
    SUPERVISED = "supervised"
    BYPASS = "bypass"


class GateError(Exception):
    """Base exception for gate failures."""
    pass


class FourEyesViolation(GateError):
    """Raised when reviewer is the same as executor."""
    pass


class SpecGateError(GateError):
    """Raised when spec gate validation fails."""
    pass


class PlanGateError(GateError):
    """Raised when plan gate fails."""
    pass


class DispatchGateError(GateError):
    """Raised when dispatch gate fails."""
    pass


class ReviewGateError(GateError):
    """Raised when review-order gate fails."""
    pass


class VerdictGateError(GateError):
    """Raised when verdict gate fails."""
    pass


class TaskState(BaseModel):
    # Input
    raw_input: str = ""

    # Task identity
    task_id: str | None = None
    project: str | None = None
    title: str | None = None

    # Workflow
    current_gate: GateType = GateType.SPEC
    status: Literal["todo", "dispatched", "in-review", "done", "changes-requested"] = "todo"
    mode: Mode = Mode.SUPERVISED

    # Gate outputs
    acceptance_criteria: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    plan: str | None = None

    # Actors
    executor: str | None = None
    reviewer: str | None = None

    # Review
    result_ref: str | None = None
    findings: list[str] = Field(default_factory=list)
    verdict: Literal["pass", "changes"] | None = None

    # Human-in-loop
    awaiting_approval: bool = False
    approval_prompt: str | None = None

    # Tracking & Metrics
    dispatched_at: str | None = None
    completed_at: str | None = None
    risk: str | None = None
    predicted_success: str | None = None
    prediction_factors: dict[str, Any] | None = None

    # Audit log
    audit_trail: list[dict[str, Any]] = Field(default_factory=list)

    # Error
    error: str | None = None
