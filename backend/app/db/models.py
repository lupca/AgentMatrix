import uuid
from sqlalchemy import Column, String, Text, Integer, Date, DateTime, ForeignKey, JSON, Boolean, CheckConstraint
from sqlalchemy.orm import validates, relationship
from sqlalchemy.sql import func
from app.db.base import Base
from app.graph.state import FourEyesViolation


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(20), primary_key=True)
    project = Column(String(50), nullable=False, index=True)
    title = Column(Text, nullable=False)
    raw_input = Column(Text, nullable=True)
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
