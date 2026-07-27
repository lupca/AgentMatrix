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
    event,
)
from sqlalchemy.orm import validates, relationship
from sqlalchemy.sql import func
from app.db.base import Base
from app.graph.state import FourEyesViolation


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


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(20), primary_key=True)
    project = Column(String(50), ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(Text, nullable=False)
    raw_input = Column(Text, nullable=True)
    tags = Column(JSON, default=list)
    status = Column(String(20), nullable=False, default="todo", index=True)
    current_gate = Column(String(20), nullable=False, default="spec", index=True)
    mode = Column(String(20), nullable=False, default="supervised")
    priority = Column(String(10), nullable=True)
    risk = Column(String(10), nullable=True)
    executor = Column(String(50), nullable=True)
    reviewer = Column(String(50), nullable=True)
    acceptance_criteria = Column(JSON, default=list)
    legacy_no_ac = Column(Boolean, nullable=False, default=False)
    files = Column(JSON, default=list)
    tests = Column(JSON, default=list)
    flows = Column(JSON, default=list)
    plan = Column(Text, nullable=True)
    result_ref = Column(String(100), nullable=True)
    findings = Column(JSON, default=list)
    verdict = Column(String(10), nullable=True)
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

    __table_args__ = (
        CheckConstraint(
            "executor IS NULL OR reviewer IS NULL "
            "OR lower(trim(executor)) <> lower(trim(reviewer))",
            name="ck_tasks_four_eyes",
        ),
        CheckConstraint(
            "status <> 'done' OR ("
            "executor IS NOT NULL AND reviewer IS NOT NULL "
            "AND lower(trim(executor)) <> lower(trim(reviewer)) "
            "AND result_ref IS NOT NULL AND trim(result_ref) <> ''"
            ")",
            name="ck_tasks_done_invariants",
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


class Session(Base):
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


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    context_md = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="active")
    repo_root = Column(String(255), nullable=True)
    task_prefix = Column(String(20), nullable=True)
    graph_status = Column(String(20), nullable=True, default="idle")
    next_task_seq = Column(Integer, nullable=False, server_default="0", default=0)
    autonomy_policy = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Agent(Base):
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
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def has_api_key(self) -> bool:
        """Expose only whether a key exists; never expose the encrypted value."""

        return bool(self.api_key)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(
        String(20),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id = Column(String(50), nullable=False)
    cli = Column(String(20), nullable=False)
    command = Column(Text, nullable=False)

    kind = Column(String(20), nullable=False, default="execute", server_default="execute")
    agent_role = Column(
        String(20), nullable=False, default="executor", server_default="executor"
    )

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

    output_lines = Column(Integer, nullable=False, default=0)
    output_bytes = Column(Integer, nullable=False, default=0)
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
    llm_usages = relationship("LLMUsage", back_populates="agent_run")

    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="ck_agent_runs_timeout_positive"),
        CheckConstraint("attempt > 0", name="ck_agent_runs_attempt_positive"),
        CheckConstraint("max_attempts > 0", name="ck_agent_runs_max_attempts_positive"),
        CheckConstraint("kind IN ('execute', 'review')", name="ck_agent_runs_kind"),
        CheckConstraint(
            "agent_role IN ('executor', 'reviewer')", name="ck_agent_runs_agent_role"
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


class Setting(Base):
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


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id = Column(String(50), primary_key=True)
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=True, default="general", index=True)
    content = Column(Text, nullable=False, default="")
    tags = Column(JSON, default=list)
    project = Column(String(50), nullable=True, index=True)
    author = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="active", server_default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
