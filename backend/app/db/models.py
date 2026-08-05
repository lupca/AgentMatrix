import uuid
from enum import Enum
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Enum as SAEnum,
    event,
)
from sqlalchemy.orm import validates, relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.compiler import compiles
from app.db.base import Base
from app.db.mixins import ArchivableMixin
from app.graph.state import FourEyesViolation


class Vector(TypeDecorator):
    """pgvector-compatible vector that remains usable in SQLite unit tests."""

    impl = Text
    cache_ok = True

    def __init__(self, dimensions: int = 1536, **kwargs):
        self.dimensions = dimensions
        super().__init__(**kwargs)

    def process_bind_param(self, value, dialect):
        if value is None or isinstance(value, str):
            return value
        return "[" + ",".join(str(float(item)) for item in value) + "]"


@compiles(Vector, "postgresql")
def _compile_vector(element, compiler, **kwargs):
    return f"vector({element.dimensions})"


class ContextLevel(str, Enum):
    GLOBAL = "global"
    PROJECT = "project"
    TASK = "task"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    CLOSED = "closed"


class AgentType(str, Enum):
    CLI = "cli"
    API = "api"


IMPL_DESIGN_JSON = JSON().with_variant(JSONB(), "postgresql")


class AgentRole(str, Enum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    COORDINATOR = "coordinator"
    SPEC_PLAN = "spec_plan"


class AgentCapability(str, Enum):
    CODE = "code"
    BACKEND = "backend"
    FRONTEND = "frontend"
    REVIEW = "review"
    RESEARCH = "research"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    COORDINATION = "coordination"
    DEVOPS = "devops"
    INFRA = "infra"
    API = "api"
    DATABASE = "database"
    DOCUMENTATION = "documentation"
    REASONING = "reasoning"
    VERIFICATION = "verification"
    DIFF_READING = "diff-reading"
    TEST_RUNNING = "test-running"
    COMPLEX_BACKEND = "complex-backend"
    COMPLEX_FRONTEND = "complex-frontend"
    COMPLEX_ANALYSIS = "complex-analysis"
    COMPLEX_LOGIC = "complex-logic"
    COMPLEX_REFACTOR = "complex-refactor"
    SIMPLE_TASKS = "simple-tasks"
    FAST = "fast"
    FAST_EXECUTION = "fast-execution"
    FAST_ITERATION = "fast-iteration"
    FULL_IMPLEMENTATION = "full-implementation"
    CLEANUP = "cleanup"
    MARKDOWN_CLEANUP = "markdown-cleanup"
    SKILL_DESIGN = "skill-design"
    SKILLS = "skills"
    SPEC_PLANNING = "spec-planning"
    DECOMPOSITION = "decomposition"
    GRAPH_SOURCING = "graph-sourcing"
    AUDIT_LOGGING = "audit-logging"
    SPOT_CHECK_RUNTIME = "spot-check-runtime"
    AC_GENERATION = "ac-generation"
    PROCESS_DESIGN = "process-design"
    CONFIRMATION = "confirmation"
    CREATIVE = "creative"
    DEEP_RESEARCH = "deep-research"
    FINAL_DECISION = "final-decision"
    FOLLOWS_EXPLICIT_INSTRUCTIONS = "follows-explicit-instructions"
    FOLLOWS_INSTRUCTIONS = "follows-instructions"
    RELIABLE = "reliable"
    CODE_REVIEW = "code-review"
    # Legacy JSON values retained as valid enum members during phase one.
    COORDINATOR = "coordinator"
    SPEC_PLAN = "spec_plan"
    EXECUTE = "execute"
    PYTHON = "python"
    REACT = "react"
    TYPESCRIPT = "typescript"
    GENERAL = "general"


class AgentRoleLink(Base):
    __tablename__ = "agent_roles"

    agent_id = Column(String(50), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    # Use String instead of SAEnum - PostgreSQL native ENUM handles validation
    role = Column(String(20), ForeignKey("role_types.role"), primary_key=True)
    agent = relationship("Agent", back_populates="agent_roles")


class AgentCapabilityLink(Base):
    __tablename__ = "agent_capabilities"

    agent_id = Column(String(50), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
    # Use String instead of SAEnum - PostgreSQL native ENUM handles validation
    capability = Column(String(50), ForeignKey("capability_types.capability"), primary_key=True)
    agent = relationship("Agent", back_populates="agent_capabilities")


class AgentRoleType(Base):
    __tablename__ = "role_types"

    # Use String - the actual ENUM constraint is in PostgreSQL
    role = Column(String(20), primary_key=True)


class AgentCapabilityType(Base):
    __tablename__ = "capability_types"

    # Use String - the actual ENUM constraint is in PostgreSQL
    capability = Column(String(50), primary_key=True)


class Task(ArchivableMixin, Base):
    """Task lifecycle projection backed by the immutable gate ledger.

    ``GateRecord`` is the source of truth for lifecycle intent and decisions.
    ``status``, ``current_gate``, and ``awaiting_approval`` are read-optimized
    projections maintained atomically with each gate record by the
    orchestration service. Clients should use :attr:`workflow_state` instead
    of inferring state from those individual fields.
    """
    __tablename__ = "tasks"

    id = Column(String(20), primary_key=True)
    project = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(Text, nullable=False)
    raw_input = Column(Text, nullable=True)
    tags = Column(JSON, default=list)
    status = Column(String(20), nullable=False, default="todo", index=True)
    version = Column(Integer, nullable=False, default=0, server_default="0")
    current_gate = Column(String(20), nullable=False, default="spec", index=True)
    mode = Column(String(20), nullable=False, default="supervised")
    priority = Column(String(10), nullable=True)
    risk = Column(String(10), nullable=True)
    spec_clarity = Column(String(10), nullable=True)
    open_questions = Column(JSON, nullable=True)
    executor = Column(String(50), nullable=True)
    reviewer = Column(String(50), nullable=True)
    acceptance_criteria = Column(JSON, default=list)
    constraints = Column(JSON, default=list)
    evidence = Column(JSON, default=list)
    prior_art = Column(JSON, default=list)
    ruled_out = Column(JSON, default=list)
    limits = Column(JSON, nullable=True)
    planner = Column(String(50), nullable=True)
    plan_critic = Column(String(50), nullable=True)
    plan_critic_status = Column(String(10), nullable=True)
    plan_critic_findings = Column(JSON, default=list)
    legacy_no_ac = Column(Boolean, nullable=False, default=False)
    files = Column(JSON, default=list)
    tests = Column(JSON, default=list)
    flows = Column(JSON, default=list)
    plan = Column(Text, nullable=True)
    coordinator_notes = Column(Text, nullable=True)
    result_ref = Column(String(100), nullable=True)
    findings = Column(JSON, default=list)
    verdict = Column(String(10), nullable=True)
    current_round_id = Column(
        String(36),
        ForeignKey("task_rounds.id", use_alter=True, name="fk_tasks_current_round_id"),
        nullable=True,
    )
    final_result_ref = Column(String(100), nullable=True)
    final_verdict = Column(String(10), nullable=True)
    # Merge commit on the integration branch once the reviewed result landed.
    # done + landed_ref = the code is actually on main (CTV2-238).
    landed_ref = Column(String(100), nullable=True)
    predicted_success = Column(String(10), nullable=True)
    prediction_factors = Column(JSON, nullable=True)
    awaiting_approval = Column(Boolean, default=False)
    approval_prompt = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    deadline = Column(Date, nullable=True)
    session_id = Column(String(36), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    sessions = relationship("Session", back_populates="task", cascade="all, delete-orphan")
    gate_records = relationship("GateRecord", back_populates="task", cascade="all, delete-orphan")
    agent_runs = relationship("AgentRun", back_populates="task", cascade="all, delete-orphan")
    llm_usages = relationship("LLMUsage", back_populates="task")
    rounds = relationship(
        "TaskRound",
        foreign_keys="TaskRound.task_id",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskRound.round_no",
    )
    current_round = relationship(
        "TaskRound", foreign_keys=[current_round_id], post_update=True
    )
    dependency_edges = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.task_id",
        cascade="all, delete-orphan",
    )
    dependent_edges = relationship(
        "TaskDependency",
        foreign_keys="TaskDependency.depends_on_task_id",
        cascade="all, delete-orphan",
    )
    notes = relationship("AgentNote", secondary="note_tasks", back_populates="tasks")
    task_events = relationship("TaskEvent", back_populates="task", cascade="all, delete-orphan")
    impl_design = relationship(
        "ImplDesign", back_populates="task", uselist=False,
        cascade="all, delete-orphan",
    )
    spec_links = relationship(
        "SpecTaskLink", back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "executor IS NULL OR reviewer IS NULL "
            "OR lower(trim(executor)) <> lower(trim(reviewer))",
            name="ck_tasks_four_eyes",
        ),
        CheckConstraint(
            "planner IS NULL OR plan_critic IS NULL "
            "OR lower(trim(planner)) <> lower(trim(plan_critic))",
            name="ck_tasks_plan_four_eyes",
        ),
        CheckConstraint(
            "plan_critic_status IS NULL OR plan_critic_status IN ('accept', 'reject')",
            name="ck_tasks_plan_critic_status",
        ),
        CheckConstraint(
            "status <> 'done' OR ("
            "executor IS NOT NULL AND reviewer IS NOT NULL "
            "AND lower(trim(executor)) <> lower(trim(reviewer)) "
            "AND result_ref IS NOT NULL AND trim(result_ref) <> ''"
            ")",
            name="ck_tasks_done_invariants",
        ),
        CheckConstraint(
            # changes-requested is NOT terminal anymore: a supervised replan
            # round parks a pending re-dispatch gate there (CTV2-234).
            "status <> 'done' OR awaiting_approval IS NOT TRUE",
            name="ck_tasks_terminal_not_awaiting_approval",
        ),
    )

    @validates("executor", "reviewer")
    def validate_four_eyes(self, key, value):
        other_key = "reviewer" if key == "executor" else "executor"
        other_val = getattr(self, other_key, None)
        if (
            value
            and other_val
            and value.strip().casefold() == other_val.strip().casefold()
        ):
            raise FourEyesViolation(
                f"Four-eyes violation: reviewer '{value}' cannot be the same as executor '{other_val}'."
            )
        return value

    @property
    def depends_on(self) -> list[str]:
        return [edge.depends_on_task_id for edge in self.dependency_edges]

    @property
    def workflow_state(self) -> str:
        """Return the stable UI lifecycle state derived from the projection."""
        if self.status in {"done", "cancelled"}:
            return "terminal"
        if self.awaiting_approval:
            return "waiting_human"
        if self.status == "failed":
            return "blocked"
        if self.status in {"awaiting-review", "in-review", "changes-requested"}:
            return "reviewing"
        return "executing"


class InboxItem(Base):
    """A raw idea captured before it becomes a task."""

    __tablename__ = "inbox_items"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    content = Column(Text, nullable=False)
    project_id = Column(String(50), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    task_id = Column(String(20), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    tags = Column(JSON, nullable=False, default=list, server_default="[]")
    status = Column(String(20), nullable=False, default="open", server_default="open")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('open', 'triaged', 'dropped')", name="ck_inbox_items_status"),
        Index("ix_inbox_items_status", "status"),
        Index("ix_inbox_items_project_id", "project_id"),
    )


class TaskDependency(Base):
    """One edge of the task DAG: ``task_id`` cannot dispatch until

    ``depends_on_task_id`` reaches ``done`` (CTV2-094). Cycle/self-loop
    rejection lives in ``TaskOrchestrationService.add_dependency`` -- this
    table only stores whatever edges pass that check.
    """

    __tablename__ = "task_dependencies"

    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "task_id <> depends_on_task_id", name="ck_task_dependencies_no_self"
        ),
        Index("ix_task_dependencies_depends_on", "depends_on_task_id"),
    )


class TaskRound(Base):
    """Per-round snapshot of a task's dispatch/review cycle (CTV2-201).

    ``Task`` overwrites executor/reviewer/result_ref on each round, losing
    history; one row here is written per execute-dispatch and updated when
    that round's verdict lands, so analytics/debugging can see every past
    round instead of only the current one.
    """

    __tablename__ = "task_rounds"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_no = Column(Integer, nullable=False)
    status = Column(String(20), nullable=False, default="dispatched")
    base_sha = Column(String(100), nullable=True)
    plan_ref = Column(String(255), nullable=True)
    # No FK to agents.id: like Task.executor/reviewer, this is a historical
    # snapshot of an agent identifier that must survive the agent being
    # renamed or deleted later.
    executor_agent_id = Column(String(50), nullable=True)
    executor_run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=True)
    reviewer_agent_id = Column(String(50), nullable=True)
    reviewer_run_id = Column(String(36), ForeignKey("agent_runs.id"), nullable=True)
    result_ref = Column(String(255), nullable=True)
    verdict = Column(String(10), nullable=True)
    findings_ref = Column(JSON, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("Task", foreign_keys=[task_id], back_populates="rounds")

    __table_args__ = (
        UniqueConstraint("task_id", "round_no", name="uq_task_rounds_task_round_no"),
    )


class DispatchDecision(Base):
    """Snapshot of an AgentMatcher scoring run behind one request_dispatch call (CTV2-202).

    Persisted before the AgentRun it may lead to exists (dispatch can stay
    pending under supervised mode), so this is the record of *why* an agent
    was picked -- and what else was considered -- independent of whether a
    run ever materializes from it.
    """

    __tablename__ = "dispatch_decisions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_round_id = Column(
        String(36),
        ForeignKey(
            "task_rounds.id", use_alter=True, name="fk_dispatch_decisions_task_round_id"
        ),
        nullable=True,
        index=True,
    )
    kind = Column(String(20), nullable=False, default="execute")
    policy_version = Column(String(50), nullable=False)
    task_feature_snapshot = Column(JSON, nullable=True)
    # No FK to agents.id: like Task.executor/reviewer, this is a historical
    # snapshot of an agent identifier that must survive the agent being
    # renamed or deleted later.
    selected_agent_id = Column(String(50), nullable=False)
    selected_score = Column(Float, nullable=True)
    selection_reason = Column(Text, nullable=True)
    exploration = Column(Boolean, nullable=False, default=False, server_default="false")
    human_override = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    candidates = relationship(
        "DispatchCandidate",
        back_populates="decision",
        cascade="all, delete-orphan",
        order_by="DispatchCandidate.id",
    )

    __table_args__ = (
        CheckConstraint("kind IN ('execute', 'review')", name="ck_dispatch_decisions_kind"),
    )


class DispatchCandidate(Base):
    """One agent's score breakdown within a DispatchDecision (CTV2-202)."""

    __tablename__ = "dispatch_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dispatch_decision_id = Column(
        String(36),
        ForeignKey("dispatch_decisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id = Column(String(50), nullable=False)
    eligible = Column(Boolean, nullable=False, default=True)
    rejection_reason = Column(Text, nullable=True)
    predicted_pass1 = Column(Float, nullable=True)
    predicted_runtime = Column(Float, nullable=True)
    quota_pressure = Column(Float, nullable=True)
    final_score = Column(Float, nullable=True)

    decision = relationship("DispatchDecision", back_populates="candidates")

    __table_args__ = (
        UniqueConstraint(
            "dispatch_decision_id", "agent_id", name="uq_dispatch_candidates_decision_agent"
        ),
    )


class GateRecord(Base):
    __tablename__ = "gate_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=False, index=True)
    gate_type = Column(String(20), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending")
    actor = Column(String(50), nullable=False, default="system")
    mode = Column(String(20), nullable=False, default="supervised")
    idempotency_key = Column(
        String(100),
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    input_hash = Column(String(64), nullable=False, default="0" * 64)
    output_ref = Column(String(255), nullable=True)
    parent_id = Column(Integer, ForeignKey("gate_records.id"), nullable=True, index=True)
    executor = Column(String(50), nullable=True)
    reviewer = Column(String(50), nullable=True)
    input_payload = Column(JSON, nullable=True)
    output_payload = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("Task", back_populates="gate_records")

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_gate_records_status",
        ),
        CheckConstraint(
            "mode IN ('supervised', 'plan-only', 'bypass')",
            name="ck_gate_records_mode",
        ),
        CheckConstraint(
            "executor IS NULL OR reviewer IS NULL "
            "OR lower(trim(executor)) <> lower(trim(reviewer))",
            name="ck_gate_records_four_eyes",
        ),
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_gate_records_task_idempotency",
        ),
    )

    @validates("executor", "reviewer")
    def validate_four_eyes(self, key, value):
        other_key = "reviewer" if key == "executor" else "executor"
        other_val = getattr(self, other_key, None)
        if (
            value
            and other_val
            and value.strip().casefold() == other_val.strip().casefold()
        ):
            raise FourEyesViolation(
                f"Four-eyes violation: reviewer '{value}' cannot be the same as executor '{other_val}'."
            )
        return value


class ImplDesign(Base):
    """The one implementation design artifact directly above a task.

    ``completeness`` is a structured, code-produced audit result.  It is not a
    model-provided score: callers should inspect ``passed`` and each check's
    concrete reason before allowing a cheap executor to run.
    """

    __tablename__ = "impl_design"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    summary = Column(Text, nullable=False, default="")
    files = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    changes = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    data_changes = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    test_plan = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    risks = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    non_goals = Column(IMPL_DESIGN_JSON, nullable=False, default=list)
    derived_from_sha = Column(String(64), nullable=True)
    authored_by = Column(String(100), nullable=False, default="unknown")
    completeness = Column(IMPL_DESIGN_JSON, nullable=True)
    reviewed_by = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("Task", back_populates="impl_design")


class ReviewCycle(Base):
    """One review pass over one task_round (CTV2-1379).

    Verdict/finding data used to live only inside gate_records.input_payload
    (JSON, unqueryable) and TaskRound.findings_ref. This is the queryable
    relational home for it. Lifecycle:
        requested -> running -> submitted -> pass | changes | abandoned
    A retry does not mutate an existing row -- it creates a NEW row against
    the new task_round; the old row keeps whatever terminal-ish status it
    last had (round_no on task_rounds already says which is newest).
    ``abandoned`` exists only for a run that died (failed/timeout/brake) with
    no verdict ever submitted -- distinguishes "still running" from "dead
    with no outcome" instead of leaving it stuck at 'running' forever.
    """

    __tablename__ = "review_cycles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable like AgentRun.task_round_id: only rows created through the
    # normal dispatch->round->review gate flow populate it; a task moved to
    # awaiting-review/in-review by an ad-hoc path (attach_result, tests, old
    # data) has no TaskRound and this stays NULL for it.
    task_round_id = Column(
        String(36), ForeignKey("task_rounds.id", ondelete="CASCADE"), nullable=True, index=True
    )
    reviewer_id = Column(String(50), nullable=True)
    reviewer_agent_run_id = Column(
        String(36), ForeignKey("agent_runs.id"), nullable=True, index=True
    )
    status = Column(String(20), nullable=False, default="requested")
    verdict = Column(String(10), nullable=True)
    # Backfill idempotency (CTV2-1379): NULL for rows created through the
    # normal review flow. Rows imported from a legacy gate_records verdict
    # payload carry the source id, guarded by a partial unique index so
    # re-running the backfill script inserts each historical verdict once.
    source_gate_record_id = Column(
        Integer, ForeignKey("gate_records.id"), nullable=True
    )
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("Task", foreign_keys=[task_id])
    task_round = relationship("TaskRound", foreign_keys=[task_round_id])
    findings = relationship(
        "ReviewFinding", back_populates="review_cycle", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'running', 'submitted', 'pass', 'changes', 'abandoned')",
            name="ck_review_cycles_status",
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict IN ('pass', 'changes')",
            name="ck_review_cycles_verdict",
        ),
        Index(
            "ix_review_cycles_source_gate_record_id_unique",
            "source_gate_record_id",
            unique=True,
            sqlite_where=source_gate_record_id.isnot(None),
            postgresql_where=source_gate_record_id.isnot(None),
        ),
    )


class ReviewFinding(Base):
    """One reviewer-reported finding, split out of TaskRound.findings_ref
    (CTV2-1379) so each finding gets its own lifecycle instead of being a
    frozen blob. ``waived`` requires a reason -- a silently dropped finding
    is indistinguishable from one nobody ever noticed.
    """

    __tablename__ = "review_findings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    review_cycle_id = Column(
        String(36), ForeignKey("review_cycles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    severity = Column(String(10), nullable=True)
    title = Column(Text, nullable=False)
    detail = Column(Text, nullable=True)
    status = Column(String(10), nullable=False, default="open")
    waived_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    review_cycle = relationship("ReviewCycle", back_populates="findings")

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'fixed', 'waived')",
            name="ck_review_findings_status",
        ),
        CheckConstraint(
            "status <> 'waived' OR (waived_reason IS NOT NULL AND trim(waived_reason) <> '')",
            name="ck_review_findings_waived_reason",
        ),
    )


@event.listens_for(GateRecord, "before_update")
def _gate_records_are_append_only(*_args) -> None:
    raise ValueError("GateRecord is append-only and cannot be updated")


@event.listens_for(GateRecord, "before_delete")
def _gate_records_cannot_be_deleted(*_args) -> None:
    raise ValueError("GateRecord is immutable and cannot be deleted")


class AdminGateRecord(Base):
    """Append-only ledger for admin-permission entity mutations (manage_project,
    manage_agent, update_settings). Mirrors GateRecord's pending/decide
    pattern but is not scoped to a Task (ADR-001 §D2)."""

    __tablename__ = "admin_gate_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity = Column(String(20), nullable=False, index=True)
    action = Column(String(20), nullable=False)
    entity_id = Column(String(50), nullable=True, index=True)
    status = Column(String(20), nullable=False, default="pending")
    actor = Column(String(50), nullable=False, default="system")
    mode = Column(String(20), nullable=False, default="supervised")
    parent_id = Column(Integer, ForeignKey("admin_gate_records.id"), nullable=True, index=True)
    input_payload = Column(JSON, nullable=True)
    output_payload = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "entity IN ('projects', 'agents', 'settings')",
            name="ck_admin_gate_records_entity",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_admin_gate_records_status",
        ),
        CheckConstraint(
            "mode IN ('supervised', 'bypass')",
            name="ck_admin_gate_records_mode",
        ),
    )


@event.listens_for(AdminGateRecord, "before_update")
def _admin_gate_records_are_append_only(*_args) -> None:
    raise ValueError("AdminGateRecord is append-only and cannot be updated")


@event.listens_for(AdminGateRecord, "before_delete")
def _admin_gate_records_cannot_be_deleted(*_args) -> None:
    raise ValueError("AdminGateRecord is immutable and cannot be deleted")


class Session(ArchivableMixin, Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=True, index=True)
    project_id = Column(
        String(50),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    context_level = Column(String(10), nullable=False, default=ContextLevel.GLOBAL.value)
    title = Column(String(200), nullable=True)
    status = Column(String(10), nullable=False, default=SessionStatus.ACTIVE.value, index=True)
    pinned = Column(Boolean, nullable=False, default=False, server_default="false")
    message_count = Column(Integer, nullable=False, default=0, server_default="0")
    thread_id = Column(String(100), nullable=True, index=True)
    current_gate = Column(String(20), nullable=True)
    checkpoint_id = Column(String(100), nullable=True, index=True)
    state_payload = Column(JSON, nullable=True)
    selected_provider = Column(String(30), nullable=True)
    selected_model = Column(String(100), nullable=True)
    messages = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now())

    task = relationship("Task", back_populates="sessions")
    project = relationship("Project", backref="sessions")
    llm_usages = relationship("LLMUsage", back_populates="session")
    event_cursor = relationship(
        "SessionEventCursor",
        back_populates="session",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "context_level IN ('global', 'project', 'task')",
            name="ck_sessions_context_level_valid",
        ),
        CheckConstraint(
            "status IN ('active', 'archived', 'closed')",
            name="ck_sessions_status_valid",
        ),
        CheckConstraint(
            "(task_id IS NULL) OR (project_id IS NOT NULL)",
            name="ck_sessions_task_requires_project",
        ),
        CheckConstraint(
            "(context_level = 'global' AND project_id IS NULL AND task_id IS NULL) OR "
            "(context_level = 'project' AND project_id IS NOT NULL AND task_id IS NULL) OR "
            "(context_level = 'task' AND project_id IS NOT NULL AND task_id IS NOT NULL)",
            name="ck_sessions_context_level_consistency",
        ),
        Index(
            "ix_sessions_context_listing",
            "context_level",
            "project_id",
            "status",
            "pinned",
            "last_activity_at",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(20), nullable=True, index=True)
    action = Column(String(50), nullable=False)
    actor = Column(String(50), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Project(ArchivableMixin, Base):
    __tablename__ = "projects"

    id = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    context_md = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    repo_root = Column(String(255), nullable=True)
    task_prefix = Column(String(20), nullable=True)
    graph_status = Column(String(20), nullable=True, default="idle")
    context_generated = Column(Boolean, nullable=False, default=False, server_default="false")
    next_task_seq = Column(Integer, nullable=False, server_default="0", default=0)
    autonomy_policy = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    rules = relationship("ProjectRule", back_populates="project", cascade="all, delete-orphan")
    notes = relationship("AgentNote", secondary="note_projects", back_populates="projects")


class ProjectRule(Base):
    __tablename__ = "project_rules"

    id = Column(String(50), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    globs = Column(JSON, nullable=False, default=list, server_default="[]")
    content = Column(Text, nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())

    project = relationship("Project", back_populates="rules")
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_project_rules_project_name"),
    )


class Agent(ArchivableMixin, Base):
    __tablename__ = "agents"

    id = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    role = Column(String(50), nullable=False)
    capabilities = Column(JSON, default=list)
    status = Column(String(20), nullable=False, default="idle")
    type = Column(String(50), nullable=True)
    model = Column(String(50), nullable=True)
    effort = Column(String(20), nullable=True)
    cli = Column(String(50), nullable=True)
    agent_type = Column(String(10), nullable=False, default=AgentType.CLI.value, server_default=AgentType.CLI.value)
    api_key = Column(String(500), nullable=True)
    provider = Column(String(50), nullable=True)
    base_url = Column(String(500), nullable=True)
    is_default = Column(Boolean, nullable=False, default=False, server_default="false")
    success_rate = Column(Float, nullable=True, default=0.0)
    # V1 markdown profile stats (total_tasks_executed/reviewed,
    # avg_review_rounds, weaknesses, recent_trend, last_active, ...) that the
    # md importer used to drop entirely.
    legacy_profile = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    agent_roles = relationship("AgentRoleLink", back_populates="agent", cascade="all, delete-orphan")
    agent_capabilities = relationship(
        "AgentCapabilityLink", back_populates="agent", cascade="all, delete-orphan"
    )

    @property
    def normalized_roles(self) -> list[str]:
        values = [link.role.value if isinstance(link.role, AgentRole) else str(link.role) for link in self.agent_roles]
        return values or ([self.role] if self.role else [])

    @property
    def normalized_capabilities(self) -> list[str]:
        values = [
            link.capability.value if isinstance(link.capability, AgentCapability) else str(link.capability)
            for link in self.agent_capabilities
        ]
        return values or list(self.capabilities or [])

    @property
    def has_api_key(self) -> bool:
        """Expose only whether a key exists; never expose the encrypted value."""

        return bool(self.api_key)


class AgentAccount(Base):
    """Health projection for one CLI subscription used by an agent."""

    __tablename__ = "agent_accounts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_id = Column(String(50), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    cli = Column(String(20), nullable=False)
    subscription_plan = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="healthy", server_default="healthy")
    quota_pressure = Column(Float, nullable=False, default=0.0, server_default="0")
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    last_rate_limit_at = Column(DateTime(timezone=True), nullable=True)
    health_score = Column(Float, nullable=False, default=1.0, server_default="1")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    agent = relationship("Agent")

    __table_args__ = (
        UniqueConstraint("agent_id", "cli", name="uq_agent_accounts_agent_cli"),
        CheckConstraint("quota_pressure >= 0 AND quota_pressure <= 1", name="ck_agent_accounts_quota_pressure"),
        CheckConstraint("health_score >= 0 AND health_score <= 1", name="ck_agent_accounts_health_score"),
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Which TaskRound (CTV2-201) this run belongs to. Nullable: only runs
    # created through the gate flow after CTV2-204 populate it -- older rows
    # and ad-hoc test fixtures leave it NULL, which never collides under the
    # uq_agent_runs_round_kind_attempt constraint below (NULLs are distinct).
    task_round_id = Column(
        String(36),
        ForeignKey("task_rounds.id", use_alter=True, name="fk_agent_runs_task_round_id"),
        nullable=True,
        index=True,
    )
    # The DispatchDecision (CTV2-202) that selected this run's agent_id, if
    # any -- only populated for runs created through request_dispatch, which
    # is the only caller that scores candidates before picking one.
    dispatch_decision_id = Column(
        String(36),
        ForeignKey(
            "dispatch_decisions.id", use_alter=True, name="fk_agent_runs_dispatch_decision_id"
        ),
        nullable=True,
        index=True,
    )
    agent_id = Column(String(50), nullable=False)
    cli = Column(String(20), nullable=False)
    command = Column(Text, nullable=False)

    # The idempotency key of the request (dispatch/review) that created this
    # run -- lets two concurrent requests for the same task race down to a
    # single row via uq_agent_runs_task_idempotency, independent of any
    # in-process locking.
    idempotency_key = Column(String(100), nullable=True)

    kind = Column(String(20), nullable=False, default="execute", server_default="execute")
    agent_role = Column(
        String(20), nullable=False, default="executor", server_default="executor"
    )
    effort = Column(String(20), nullable=True)

    status = Column(String(20), nullable=False, default="queued", index=True)
    pid = Column(Integer, nullable=True)
    dramatiq_message_id = Column(String(50), nullable=True)

    queued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=900)

    exit_code = Column(Integer, nullable=True)
    result_ref = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    # A deliberately low-cardinality attribution of how the run ended.  The
    # default is unknown: an exit code or free-form stderr is not evidence of
    # agent fault.  Historical rows are marked by the migration's data-quality
    # column and are never relabelled from their old evidence.
    failure_category = Column(
        String(30), nullable=False, default="unknown", server_default="unknown", index=True
    )
    failure_data_quality = Column(
        String(20), nullable=False, default="current", server_default="current", index=True
    )

    output_lines = Column(Integer, nullable=False, default=0)
    output_bytes = Column(Integer, nullable=False, default=0)
    next_event_seq = Column(Integer, nullable=False, default=0, server_default="0")
    attempt = Column(Integer, nullable=False, default=1)
    max_attempts = Column(Integer, nullable=False, default=3)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    task = relationship("Task", back_populates="agent_runs")
    output_chunks = relationship(
        "AgentOutputChunk",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentOutputChunk.chunk_index",
    )
    agent_events = relationship(
        "AgentEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="AgentEvent.seq",
    )
    vendor_raw_events = relationship(
        "VendorRawEvent",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="VendorRawEvent.seq",
    )
    llm_usages = relationship("LLMUsage", back_populates="agent_run")
    resource_usage = relationship(
        "RunResourceUsage", back_populates="agent_run", uselist=False,
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="ck_agent_runs_timeout_positive"),
        CheckConstraint("attempt > 0", name="ck_agent_runs_attempt_positive"),
        CheckConstraint("max_attempts > 0", name="ck_agent_runs_max_attempts_positive"),
        CheckConstraint("kind IN ('execute', 'review')", name="ck_agent_runs_kind"),
        CheckConstraint(
            "agent_role IN ('executor', 'reviewer')", name="ck_agent_runs_agent_role"
        ),
        CheckConstraint(
            "failure_category IN ("
            "'infra_timeout', 'infra_config', 'infra_conflict', 'infra_parse', "
            "'agent_no_output', 'agent_wrong', 'agent_incomplete', "
            "'brake_stopped', 'cancelled', 'unknown')",
            name="ck_agent_runs_failure_category",
        ),
        CheckConstraint(
            "failure_data_quality IN ('current', 'legacy')",
            name="ck_agent_runs_failure_data_quality",
        ),
        UniqueConstraint(
            "task_round_id",
            "kind",
            "attempt",
            name="uq_agent_runs_round_kind_attempt",
        ),
        UniqueConstraint(
            "task_id",
            "idempotency_key",
            name="uq_agent_runs_task_idempotency",
        ),
    )


class AgentOutputChunk(Base):
    __tablename__ = "agent_output_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run = relationship("AgentRun", back_populates="output_chunks")

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_output_chunks_index_nonnegative"),
        UniqueConstraint("run_id", "chunk_index", name="uq_output_chunks_run_index"),
    )


class RunResourceUsage(Base):
    """Aggregated resource counters for one completed agent run."""

    __tablename__ = "run_resource_usage"

    agent_run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), primary_key=True)
    llm_calls = Column(Integer, nullable=False, default=0, server_default="0")
    input_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    output_tokens = Column(Integer, nullable=False, default=0, server_default="0")
    tool_calls = Column(Integer, nullable=False, default=0, server_default="0")
    bash_commands = Column(Integer, nullable=False, default=0, server_default="0")
    files_read = Column(Integer, nullable=False, default=0, server_default="0")
    files_written = Column(Integer, nullable=False, default=0, server_default="0")
    active_seconds = Column(Float, nullable=False, default=0.0, server_default="0")
    rate_limit_events = Column(Integer, nullable=False, default=0, server_default="0")
    estimated_cost_usd = Column(Numeric(14, 8), nullable=False, default=0, server_default="0")

    agent_run = relationship("AgentRun", back_populates="resource_usage")

    __table_args__ = (
        CheckConstraint("llm_calls >= 0", name="ck_run_resource_usage_llm_calls"),
        CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_run_resource_usage_tokens"),
        CheckConstraint("estimated_cost_usd >= 0", name="ck_run_resource_usage_cost"),
    )


AGENT_EVENT_TYPES = (
    "run.started", "llm.requested", "llm.completed", "tool.started",
    "tool.completed", "gate.requested", "workspace.changed",
    "run.heartbeat", "run.completed",
)


class AgentEvent(Base):
    """Vendor-independent event emitted by an agent execution (CTV2-209)."""

    __tablename__ = "agent_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    event_type = Column(String(30), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)

    run = relationship("AgentRun", back_populates="agent_events")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('run.started', 'llm.requested', 'llm.completed', "
            "'tool.started', 'tool.completed', 'gate.requested', "
            "'workspace.changed', 'run.heartbeat', 'run.completed')",
            name="ck_agent_events_type",
        ),
        CheckConstraint("seq >= 0", name="ck_agent_events_seq_nonnegative"),
        UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),
        Index("idx_agent_events_run_type", "run_id", "event_type"),
    )


class VendorRawEvent(Base):
    """Original CLI output retained alongside its normalized interpretation."""

    __tablename__ = "vendor_raw_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    cli = Column(String(20), nullable=False)
    raw_output = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    run = relationship("AgentRun", back_populates="vendor_raw_events")

    __table_args__ = (
        CheckConstraint("seq >= 0", name="ck_vendor_raw_events_seq_nonnegative"),
        UniqueConstraint("run_id", "seq", name="uq_vendor_raw_events_run_seq"),
    )


class OutboxEvent(Base):
    """Transactional outbox for reliably handing a queued run off to Dramatiq (CTV2-205).

    `TaskOrchestrationService._apply_gate` inserts one of these in the same
    DB transaction (and commit) as the `AgentRun` row it describes, so a
    crash between "AgentRun committed" and "Dramatiq message sent" can never
    strand a run: `app.services.outbox.publish_pending_events` (polled by
    `app.workers.outbox_publisher`) picks up any row still unpublished and
    (re)sends it. `last_attempted_at` drives the retry backoff and
    `attempts`/`dead_letter` cap it -- see `app.services.outbox` for the
    policy.
    """

    __tablename__ = "outbox_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String(30), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    published_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    dead_letter = Column(Boolean, nullable=False, default=False, server_default="false")

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('run_requested', 'graph_rebuild_requested')",
            name="ck_outbox_events_type",
        ),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts_nonnegative"),
        Index(
            "ix_outbox_events_unpublished",
            "dead_letter",
            "published_at",
            "created_at",
        ),
    )


class LLMUsage(Base):
    """Immutable token and cost ledger for one provider request."""

    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    task_id = Column(
        String(20),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    agent_run_id = Column(
        String(36),
        ForeignKey("agent_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model = Column(String(100), nullable=False, index=True)
    provider = Column(String(30), nullable=False, index=True)
    operation = Column(String(30), nullable=False, index=True)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cached_tokens = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Numeric(14, 8), nullable=False, default=0)
    latency_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    session = relationship("Session", back_populates="llm_usages")
    task = relationship("Task", back_populates="llm_usages")
    agent_run = relationship("AgentRun", back_populates="llm_usages")

    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="ck_llm_usage_input_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="ck_llm_usage_output_nonnegative"),
        CheckConstraint("cached_tokens >= 0", name="ck_llm_usage_cached_nonnegative"),
        CheckConstraint("cost_usd >= 0", name="ck_llm_usage_cost_nonnegative"),
        CheckConstraint("latency_ms >= 0", name="ck_llm_usage_latency_nonnegative"),
    )


class ModelPricing(Base):
    """LLM model pricing table for cost calculations."""

    __tablename__ = "model_pricing"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(100), nullable=False)
    provider = Column(String(50), nullable=False)
    input_price_per_mtok = Column(Numeric(10, 4), nullable=True)
    output_price_per_mtok = Column(Numeric(10, 4), nullable=True)
    cached_input_price_per_mtok = Column(Numeric(10, 4), nullable=True)
    cache_write_5m_per_mtok = Column(Numeric(10, 4), nullable=True)
    cache_write_1h_per_mtok = Column(Numeric(10, 4), nullable=True)
    notes = Column(Text, nullable=True)
    effective_from = Column(Date, server_default=func.current_date())
    effective_until = Column(Date, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("model", "provider", "effective_from", name="uq_model_pricing_unique"),
    )


class Setting(ArchivableMixin, Base):
    """Whitelisted system-configuration KV store (ADR-001 §D2 Phase 2d).

    Only keys in ``entity_admin.SETTINGS_WHITELIST`` may be written; enforced
    in the service layer, not here, so the whitelist can grow without a
    migration.
    """

    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=True)
    description = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class KnowledgeItem(ArchivableMixin, Base):
    __tablename__ = "knowledge_items"

    id = Column(String(50), primary_key=True)
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=True, default="general", index=True)
    content = Column(Text, nullable=False, default="")
    tags = Column(JSON, default=list)
    project = Column(String(50), nullable=True, index=True)
    author = Column(String(50), nullable=True)
    embedding = Column(Vector(2560), nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SpecItem(ArchivableMixin, Base):
    """A versioned, queryable proposition in the living specification."""

    __tablename__ = "spec_item"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(
        String(50), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind = Column(String(20), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="draft", server_default="draft", index=True)
    # Set only by the pure-code invalidation engine (app.services.spec_anchor)
    # when a commit touches an anchored symbol -- never by an LLM. Records
    # which symbol, in which commit, so an agent never has to ask "is this
    # still true" (CTV2-1342).
    stale_reason = Column(Text, nullable=True)
    # Independent axis from `status`: whether the claim has become code, not
    # whether the claim is still correct. Always 'agreed' at write time and
    # never updated by a column write -- `spec_get` derives the live value
    # from anchors + linked task status on every read (CTV2-1395). Never set
    # by spec_write; see the reject-realization guard in spec_service.
    realization = Column(
        String(10), nullable=False, default="agreed", server_default="agreed"
    )
    supersedes_id = Column(
        String(36), ForeignKey("spec_item.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_doc_id = Column(String(50), nullable=True, index=True)
    derived_from_sha = Column(String(64), nullable=True)
    derived_by = Column(String(100), nullable=True)
    confidence = Column(String(20), nullable=False, default="asserted", server_default="asserted")
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(String(100), nullable=True)
    embedding = Column(Vector(1536), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    project = relationship("Project")
    supersedes = relationship("SpecItem", remote_side=[id], back_populates="superseded_by")
    superseded_by = relationship("SpecItem", back_populates="supersedes")
    outgoing_relations = relationship(
        "SpecRelation",
        foreign_keys="SpecRelation.from_id",
        back_populates="from_item",
        cascade="all, delete-orphan",
    )
    incoming_relations = relationship(
        "SpecRelation",
        foreign_keys="SpecRelation.to_id",
        back_populates="to_item",
        cascade="all, delete-orphan",
    )
    anchors = relationship(
        "SpecAnchor", back_populates="spec_item", cascade="all, delete-orphan"
    )
    task_links = relationship(
        "SpecTaskLink", back_populates="spec_item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "realization IN ('agreed', 'built')", name="ck_spec_item_realization"
        ),
    )


class SpecAnchor(Base):
    """A pure-code anchor from a spec item to a repo/path/symbol (CTV2-1342).

    ``anchor_sha`` hashes the canonical anchored content at anchor time (see
    ``app.services.spec_anchor.compute_anchor_sha``): a local Python AST
    declaration for ``.py`` files, or the whole file for every other path.
    ``spec_write`` always computes it on the server and ignores a supplied
    value when the source is available; a supplied value is only a
    compatibility fallback for a repo that is not checked out and must be a
    64-character hexadecimal content hash.
    The commit-triggered invalidation engine recomputes the same hash at the
    changed commit and flips the owning ``spec_item`` to ``stale`` when it no
    longer matches; the anchor row itself is never rewritten by that engine,
    only compared against.
    """

    __tablename__ = "spec_anchor"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    spec_item_id = Column(
        String(36), ForeignKey("spec_item.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repo = Column(String(255), nullable=False)
    path = Column(String(500), nullable=False)
    symbol = Column(String(300), nullable=False)
    relation = Column(String(20), nullable=False)
    anchor_sha = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    spec_item = relationship("SpecItem", back_populates="anchors")

    __table_args__ = (
        CheckConstraint(
            "relation IN ('implements', 'constrains', 'tests', 'documents')",
            name="ck_spec_anchor_relation",
        ),
        UniqueConstraint(
            "spec_item_id", "repo", "path", "symbol", "relation", name="uq_spec_anchor_target"
        ),
        Index("ix_spec_anchor_repo_path", "repo", "path"),
    )


class SpecRelation(Base):
    """A typed edge between two active or historical spec items."""

    __tablename__ = "spec_relation"

    from_id = Column(
        String(36), ForeignKey("spec_item.id", ondelete="CASCADE"), primary_key=True
    )
    to_id = Column(
        String(36), ForeignKey("spec_item.id", ondelete="CASCADE"), primary_key=True
    )
    kind = Column(String(20), primary_key=True)

    from_item = relationship(
        "SpecItem", foreign_keys=[from_id], back_populates="outgoing_relations"
    )
    to_item = relationship(
        "SpecItem", foreign_keys=[to_id], back_populates="incoming_relations"
    )


class SpecTaskLink(Base):
    """A manual or landing-derived typed edge between a project spec and task."""

    __tablename__ = "spec_task_link"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    spec_item_id = Column(
        String(36), ForeignKey("spec_item.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    relation = Column(String(20), nullable=False)
    confidence = Column(String(20), nullable=False, default="asserted", server_default="asserted")
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    spec_item = relationship("SpecItem", back_populates="task_links")
    task = relationship("Task", back_populates="spec_links")

    __table_args__ = (
        CheckConstraint(
            "relation IN ('implements', 'modifies', 'violates', 'references')",
            name="ck_spec_task_link_relation",
        ),
        CheckConstraint(
            "confidence IN ('asserted', 'derived', 'verified')",
            name="ck_spec_task_link_confidence",
        ),
        UniqueConstraint(
            "spec_item_id", "task_id", "relation", name="uq_spec_task_link_edge"
        ),
    )


class AgentNote(ArchivableMixin, Base):
    __tablename__ = "agent_notes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(300), nullable=False)
    content = Column(Text, nullable=False)
    note_type = Column(String(30), nullable=False, default="fact", server_default="fact")
    tags = Column(JSON, nullable=False, default=list, server_default="[]")
    embedding = Column(Vector(2560), nullable=True)
    author = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    projects = relationship("Project", secondary="note_projects", back_populates="notes")
    tasks = relationship("Task", secondary="note_tasks", back_populates="notes")


class NoteProject(Base):
    __tablename__ = "note_projects"

    note_id = Column(String(36), ForeignKey("agent_notes.id", ondelete="CASCADE"), primary_key=True)
    project_id = Column(String(50), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)


class NoteTask(Base):
    __tablename__ = "note_tasks"

    note_id = Column(String(36), ForeignKey("agent_notes.id", ondelete="CASCADE"), primary_key=True)
    task_id = Column(String(20), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)


class TaskEvent(Base):
    """Single source of truth for task state change events (CTV2-114)."""

    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        String(20),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type = Column(String(30), nullable=False)
    kind = Column(String(10), nullable=False, server_default="info")
    claimed_by_session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    consumed_at = Column(DateTime(timezone=True), nullable=True)

    task = relationship("Task", back_populates="task_events")
    claimed_by_session = relationship("Session")

    __table_args__ = (
        Index("idx_task_events_type_created", "event_type", "created_at"),
        CheckConstraint(
            "kind IN ('decision', 'info')",
            name="ck_task_events_kind_valid",
        ),
        Index(
            "idx_task_events_decision_claim",
            "kind",
            "claimed_by_session_id",
            postgresql_where=(kind == "decision"),
            sqlite_where=(kind == "decision"),
        ),
    )


class TaskOwner(Base):
    """Who last touched a task via a state-changing MCP tool (CTV2-1399).

    Last writer wins: one row per task, overwritten whenever a registered
    (mutating) tool call succeeds against it. ``session_id`` NULL, or a
    session whose ``last_activity_at`` has gone stale, reads as "vô chủ" --
    the task is then surfaced to every session instead of nobody.
    """

    __tablename__ = "task_owners"

    task_id = Column(
        String(20), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    session_id = Column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SessionEventCursor(Base):
    """Per-session cursor for digesting informational task events."""

    __tablename__ = "session_event_cursors"

    session_id = Column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_digest_event_id = Column(Integer, nullable=False, server_default="0", default=0)

    session = relationship("Session", back_populates="event_cursor")


class ToolMetric(Base):
    """One row per invocation of a token-saving tool (graph, ocr, review).

    The system was blind: graph failures degraded to [] with only a log
    line, and nothing recorded whether these tools are used at all, how
    often they succeed, or how much context they return. Analyzable via
    query_db (SELECT ... FROM tool_metrics).
    """

    __tablename__ = "tool_metrics"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tool = Column(String(50), nullable=False, index=True)
    source = Column(String(30), nullable=False)
    task_id = Column(String(50), nullable=True)
    ok = Column(Boolean, nullable=False)
    cache_hit = Column(Boolean, nullable=False, default=False)
    duration_ms = Column(Integer, nullable=True)
    result_count = Column(Integer, nullable=True)
    bytes_out = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    payload = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class NotificationDelivery(Base):
    """Append-only ledger for Telegram notification deliveries (CTV2-1381).

    One row per (task_event → Telegram message) attempt.  The HTTP call to
    api.telegram.org happens outside any DB transaction: the row is claimed
    (INSERT + COMMIT), the session released, the send performed, then a
    second short session writes the outcome.  Telegram failure is recorded
    here, never propagated to task state.
    """

    __tablename__ = "notification_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        String(20),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    task_event_id = Column(
        Integer,
        ForeignKey("task_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    channel = Column(String(20), nullable=False, default="telegram")
    chat_id = Column(String(50), nullable=True)
    correlation_token = Column(
        String(36),
        nullable=False,
        default=lambda: str(uuid.uuid4()),
        unique=True,
    )
    status = Column(String(10), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    last_error = Column(Text, nullable=True)
    provider_message_id = Column(String(50), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_notification_deliveries_status",
        ),
    )
