import uuid
from sqlalchemy import Column, String, Text, Integer, Date, DateTime, ForeignKey, JSON
from sqlalchemy.sql import func
from app.db.base import Base

class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(20), primary_key=True)
    project = Column(String(50), nullable=False, index=True)
    title = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="todo", index=True)
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
    deadline = Column(Date, nullable=True)
    session_id = Column(String(36), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=True, index=True)
    thread_id = Column(String(100), nullable=True)
    current_gate = Column(String(20), nullable=True)
    messages = Column(JSON, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String(20), nullable=True, index=True)
    action = Column(String(50), nullable=False)
    actor = Column(String(50), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
