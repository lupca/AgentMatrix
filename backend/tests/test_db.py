import os
import pytest
from datetime import date
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SQLAlchemySession

from app.db.base import engine, SessionLocal
from app.db.models import Task, Session, AuditLog


def test_engine_pool_configuration():
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 10


def test_create_and_query_task(db_session):
    """Test basic task/session/audit CRUD using the SQLite fixture."""
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
    db_session.add(task)
    db_session.commit()

    # Query task
    fetched_task = db_session.query(Task).filter(Task.id == "CTV2-001").first()
    assert fetched_task is not None
    assert fetched_task.project == "control-tower-v2"
    assert fetched_task.status == "dispatched"
    assert fetched_task.acceptance_criteria == ["PostgreSQL container", "SQLAlchemy models"]

    # Create session linked to task
    session_obj = Session(
        task_id="CTV2-001",
        project_id="control-tower-v2",
        context_level="task",
        thread_id="thread-123",
        current_gate="dispatch",
        messages=[{"role": "user", "content": "Execute task"}]
    )
    db_session.add(session_obj)

    # Create audit log
    audit = AuditLog(
        task_id="CTV2-001",
        action="DISPATCH",
        actor="@antigravity",
        details={"status": "dispatched"}
    )
    db_session.add(audit)
    db_session.commit()

    # Query session and audit log
    fetched_session = db_session.query(Session).filter(Session.task_id == "CTV2-001").first()
    assert fetched_session is not None
    assert fetched_session.thread_id == "thread-123"

    fetched_audit = db_session.query(AuditLog).filter(AuditLog.task_id == "CTV2-001").first()
    assert fetched_audit is not None
    assert fetched_audit.action == "DISPATCH"


def test_session_valid_context_levels(db_session):
    global_session = Session(context_level="global")
    project_session = Session(context_level="project", project_id="proj-1")
    task_session = Session(context_level="task", project_id="proj-1", task_id="T-1")
    db_session.add_all([global_session, project_session, task_session])
    db_session.commit()

    assert global_session.status == "active"
    assert global_session.pinned is False
    assert global_session.message_count == 0
    assert project_session.project_id == "proj-1"
    assert task_session.task_id == "T-1"


def test_session_context_level_consistency_constraint(db_session):
    db_session.add(Session(context_level="task", task_id=None, project_id=None))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_session_task_requires_project_constraint(db_session):
    db_session.add(Session(context_level="global", task_id="T-1", project_id=None))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_session_invalid_context_level_value_constraint(db_session):
    db_session.add(Session(context_level="bogus"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_setting_key_is_primary_key_and_value_is_json(db_session):
    from app.db.models import Setting

    db_session.add(Setting(key="context_snapshot_top_n", value=20, description="desc"))
    db_session.commit()

    setting = db_session.get(Setting, "context_snapshot_top_n")
    assert setting.value == 20
    assert setting.updated_at is not None


def test_admin_gate_record_accepts_settings_entity(db_session):
    from app.db.models import AdminGateRecord

    record = AdminGateRecord(
        entity="settings",
        action="update",
        entity_id="context_snapshot_top_n",
        status="approved",
        actor="system",
        mode="bypass",
        input_payload={"value": 20},
    )
    db_session.add(record)
    db_session.commit()

    assert db_session.get(AdminGateRecord, record.id).entity == "settings"


def test_admin_gate_record_rejects_unknown_entity_constraint(db_session):
    from app.db.models import AdminGateRecord

    db_session.add(
        AdminGateRecord(entity="bogus", action="update", status="pending", actor="system")
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
