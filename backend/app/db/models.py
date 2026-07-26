import uuid
from sqlalchemy import Column, String, Text, Integer, Date, DateTime, ForeignKey, JSON, Boolean, CheckConstraint, Float, UniqueConstraint
from sqlalchemy.orm import validates, relationship
from sqlalchemy.sql import func
from app.db.base import Base
from app.graph.state import FourEyesViolation


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

    __table_args__ = (
        CheckConstraint("executor IS NULL OR reviewer IS NULL OR executor <> reviewer", name="ck_tasks_four_eyes"),
    )

    @validates("executor", "reviewer")
    def validate_four_eyes(self, key, value):
        other_key = "reviewer" if key == "executor" else "executor"
        other_val = getattr(self, other_key, None)
        if value and other_val and value == other_val:
            raise FourEyesViolation(
                f"Four-eyes violation: reviewer '{value}' cannot be the same as executor '{other_val}'."
            )
        return value


class GateRecord(Base):
    __tablename__ = "gate_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=False, index=True)
    gate_type = Column(String(20), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending")
    executor = Column(String(50), nullable=True)
    reviewer = Column(String(50), nullable=True)
    input_payload = Column(JSON, nullable=True)
    output_payload = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("Task", back_populates="gate_records")

    __table_args__ = (
        CheckConstraint("executor IS NULL OR reviewer IS NULL OR executor <> reviewer", name="ck_gate_records_four_eyes"),
    )

    @validates("executor", "reviewer")
    def validate_four_eyes(self, key, value):
        other_key = "reviewer" if key == "executor" else "executor"
        other_val = getattr(self, other_key, None)
        if value and other_val and value == other_val:
            raise FourEyesViolation(
                f"Four-eyes violation: reviewer '{value}' cannot be the same as executor '{other_val}'."
            )
        return value


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=True, index=True)
    thread_id = Column(String(100), nullable=True, index=True)
    current_gate = Column(String(20), nullable=True)
    checkpoint_id = Column(String(100), nullable=True, index=True)
    state_payload = Column(JSON, nullable=True)
    messages = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    task = relationship("Task", back_populates="sessions")


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
    status = Column(String(20), nullable=False, default="active")
    repo_root = Column(String(255), nullable=True)
    task_prefix = Column(String(20), nullable=True)
    graph_status = Column(String(20), nullable=True, default="idle")
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
    success_rate = Column(Float, nullable=True, default=0.0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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

    status = Column(String(20), nullable=False, default="queued", index=True)
    pid = Column(Integer, nullable=True)
    dramatiq_message_id = Column(String(50), nullable=True)

    queued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    timeout_seconds = Column(Integer, nullable=False, default=14_400)

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

    __table_args__ = (
        CheckConstraint("timeout_seconds > 0", name="ck_agent_runs_timeout_positive"),
        CheckConstraint("attempt > 0", name="ck_agent_runs_attempt_positive"),
        CheckConstraint("max_attempts > 0", name="ck_agent_runs_max_attempts_positive"),
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


class KnowledgeItem(Base):
    __tablename__ = "knowledge_items"

    id = Column(String(50), primary_key=True)
    title = Column(String(200), nullable=False)
    category = Column(String(50), nullable=True, default="general", index=True)
    content = Column(Text, nullable=False, default="")
    tags = Column(JSON, default=list)
    project = Column(String(50), nullable=True, index=True)
    author = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
