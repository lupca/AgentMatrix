import os
import pytest
from datetime import date
from sqlalchemy.orm import Session as SQLAlchemySession

from app.db.base import engine, SessionLocal
from app.db.models import Task, Session, AuditLog


def test_engine_pool_configuration():
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 10


def test_create_and_query_task():
    db: SQLAlchemySession = SessionLocal()
    try:
        # Clean up any leftover from previous runs
        db.query(Session).filter(Session.task_id == "CTV2-001").delete()
        db.query(AuditLog).filter(AuditLog.task_id == "CTV2-001").delete()
        db.query(Task).filter(Task.id == "CTV2-001").delete()
        db.commit()

        # Create task
        task = Task(
            id="CTV2-001",
            project="control-tower-v2",
            title="Database Schema + Alembic Migrations",
            status="dispatched",
            priority="high",
            risk="low",
            executor="@antigravity-3.6-high",
            reviewer="@antigravity",
            acceptance_criteria=["PostgreSQL container", "SQLAlchemy models"],
            files=["backend/app/db/models.py"],
            tests=["backend/tests/test_db.py"],
            plan="Setup DB and models",
            deadline=date(2026, 8, 5),
        )
        db.add(task)
        db.commit()

        # Query task
        fetched_task = db.query(Task).filter(Task.id == "CTV2-001").first()
        assert fetched_task is not None
        assert fetched_task.project == "control-tower-v2"
        assert fetched_task.status == "dispatched"
        assert fetched_task.acceptance_criteria == ["PostgreSQL container", "SQLAlchemy models"]

        # Create session linked to task
        session_obj = Session(
            task_id="CTV2-001",
            thread_id="thread-123",
            current_gate="dispatch",
            messages=[{"role": "user", "content": "Execute task"}]
        )
        db.add(session_obj)

        # Create audit log
        audit = AuditLog(
            task_id="CTV2-001",
            action="DISPATCH",
            actor="@antigravity",
            details={"status": "dispatched"}
        )
        db.add(audit)
        db.commit()

        # Query session and audit log
        fetched_session = db.query(Session).filter(Session.task_id == "CTV2-001").first()
        assert fetched_session is not None
        assert fetched_session.thread_id == "thread-123"

        fetched_audit = db.query(AuditLog).filter(AuditLog.task_id == "CTV2-001").first()
        assert fetched_audit is not None
        assert fetched_audit.action == "DISPATCH"

    finally:
        # Clean up
        db.query(Session).filter(Session.task_id == "CTV2-001").delete()
        db.query(AuditLog).filter(AuditLog.task_id == "CTV2-001").delete()
        db.query(Task).filter(Task.id == "CTV2-001").delete()
        db.commit()
        db.close()
