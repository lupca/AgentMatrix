import os
import pytest
from datetime import date, datetime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from alembic.config import Config
from alembic import command

from app.db.base import Base
from app.db.models import Task, GateRecord, Session, AuditLog, TaskDependency
from app.graph.state import FourEyesViolation
from app.schemas.task import TaskState, GateRecordCreate, GateRecord as GateRecordSchema


# ---------------------------------------------------------------------------
# Category 1: Alembic Migration & Table Structure Tests
# ---------------------------------------------------------------------------

def test_alembic_migration_head():
    """Verify that Alembic migration '001_initial' applies cleanly to an empty database."""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    connection = engine.connect()

    # Setup Alembic configuration pointing to backend/alembic.ini
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ini_path = os.path.join(backend_dir, "alembic.ini")
    alembic_cfg = Config(ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))

    # Run migration upgrade with connection override
    alembic_cfg.attributes["connection"] = connection
    command.upgrade(alembic_cfg, "head")

    inspector = inspect(connection)
    tables = inspector.get_table_names()

    # Check that all required tables exist
    assert "tasks" in tables
    assert "gate_records" in tables
    assert "sessions" in tables
    assert "audit_log" in tables
    assert "task_dependencies" in tables

    # Inspect task_dependencies columns
    dep_cols = {col["name"]: col for col in inspector.get_columns("task_dependencies")}
    assert "task_id" in dep_cols
    assert "depends_on_task_id" in dep_cols
    pk_cols = set(inspector.get_pk_constraint("task_dependencies")["constrained_columns"])
    assert pk_cols == {"task_id", "depends_on_task_id"}

    # Inspect tasks columns
    task_cols = {col["name"]: col for col in inspector.get_columns("tasks")}
    assert "raw_input" in task_cols
    assert "current_gate" in task_cols
    assert "mode" in task_cols
    assert "awaiting_approval" in task_cols
    assert "approval_prompt" in task_cols
    assert "error" in task_cols
    assert "session_id" in task_cols

    # Inspect gate_records columns
    gate_cols = {col["name"]: col for col in inspector.get_columns("gate_records")}
    assert "task_id" in gate_cols
    assert "gate_type" in gate_cols
    assert "status" in gate_cols
    assert "executor" in gate_cols
    assert "reviewer" in gate_cols
    assert "input_payload" in gate_cols
    assert "output_payload" in gate_cols
    assert "error_message" in gate_cols

    # Inspect sessions columns
    session_cols = {col["name"]: col for col in inspector.get_columns("sessions")}
    assert "checkpoint_id" in session_cols
    assert "state_payload" in session_cols
    assert "thread_id" in session_cols

    connection.close()


# ---------------------------------------------------------------------------
# Category 2: SQLAlchemy Model CRUD & Relationships
# ---------------------------------------------------------------------------

def test_task_model_v2_crud(db_session):
    """Verify CRUD operations on Task model with V2 TaskState fields."""
    task = Task(
        id="V2-001",
        project="control-tower-v2",
        title="V2 Schema Task Test",
        raw_input="System requirement specification text",
        status="todo",
        current_gate="spec",
        mode="supervised",
        priority="high",
        risk="medium",
        executor="@builder",
        reviewer="@checker",
        acceptance_criteria=["AC-1", "AC-2"],
        files=["backend/app/db/models.py"],
        tests=["backend/tests/test_db_v2.py"],
        plan="Migration & schema update",
        awaiting_approval=True,
        approval_prompt="Please approve spec output",
        error=None,
        deadline=date(2026, 8, 1)
    )
    db_session.add(task)
    db_session.commit()

    fetched = db_session.query(Task).filter(Task.id == "V2-001").first()
    assert fetched is not None
    assert fetched.raw_input == "System requirement specification text"
    assert fetched.current_gate == "spec"
    assert fetched.mode == "supervised"
    assert fetched.awaiting_approval is True
    assert fetched.approval_prompt == "Please approve spec output"
    assert fetched.executor == "@builder"
    assert fetched.reviewer == "@checker"

    # Update Task
    fetched.current_gate = "plan"
    fetched.awaiting_approval = False
    fetched.status = "dispatched"
    db_session.commit()

    updated = db_session.query(Task).filter(Task.id == "V2-001").first()
    assert updated.current_gate == "plan"
    assert updated.awaiting_approval is False
    assert updated.status == "dispatched"


def test_gate_record_crud_and_relationship(db_session):
    """Verify CRUD and Task relationship on GateRecord model."""
    task = Task(
        id="V2-002",
        project="control-tower-v2",
        title="Gate Record Task",
        executor="@dev-1",
        reviewer="@dev-2"
    )
    db_session.add(task)
    db_session.commit()

    gate_rec = GateRecord(
        task_id="V2-002",
        gate_type="spec",
        status="approved",
        executor="@dev-1",
        reviewer="@dev-2",
        input_payload={"raw_input": "Build feature X"},
        output_payload={"criteria": ["Feature X unit tests pass"]},
        error_message=None
    )
    db_session.add(gate_rec)
    db_session.commit()

    fetched_gate = db_session.query(GateRecord).filter(GateRecord.task_id == "V2-002").first()
    assert fetched_gate is not None
    assert fetched_gate.gate_type == "spec"
    assert fetched_gate.status == "approved"
    assert fetched_gate.input_payload == {"raw_input": "Build feature X"}
    assert fetched_gate.output_payload == {"criteria": ["Feature X unit tests pass"]}
    assert fetched_gate.task.title == "Gate Record Task"

    # Verify back-populates relationship on Task
    db_session.refresh(task)
    assert len(task.gate_records) == 1
    assert task.gate_records[0].id == fetched_gate.id


def test_session_checkpoint_and_relationship(db_session):
    """Verify Session checkpoint fields and relationship with Task."""
    task = Task(
        id="V2-003",
        project="control-tower-v2",
        title="Session Task"
    )
    db_session.add(task)
    db_session.commit()

    sess = Session(
        task_id="V2-003",
        project_id="control-tower-v2",
        context_level="task",
        thread_id="thread-v2-003",
        current_gate="dispatch",
        checkpoint_id="chk-9999",
        state_payload={"current_gate": "dispatch", "status": "dispatched"},
        messages=[{"role": "user", "content": "Run dispatch"}]
    )
    db_session.add(sess)
    db_session.commit()

    fetched_sess = db_session.query(Session).filter(Session.task_id == "V2-003").first()
    assert fetched_sess is not None
    assert fetched_sess.checkpoint_id == "chk-9999"
    assert fetched_sess.state_payload == {"current_gate": "dispatch", "status": "dispatched"}
    assert fetched_sess.thread_id == "thread-v2-003"
    assert fetched_sess.task.id == "V2-003"

    db_session.refresh(task)
    assert len(task.sessions) == 1
    assert task.sessions[0].checkpoint_id == "chk-9999"


def test_gate_ledger_blocks_parent_task_deletion(db_session):
    """Immutable gate evidence prevents deletion through an ORM cascade."""
    task = Task(id="V2-CASCADE", project="proj", title="Cascade Task")
    db_session.add(task)
    db_session.commit()

    gate = GateRecord(task_id="V2-CASCADE", gate_type="spec", status="pending")
    sess = Session(
        task_id="V2-CASCADE",
        project_id="proj",
        context_level="task",
        thread_id="t-cas",
    )
    db_session.add_all([gate, sess])
    db_session.commit()

    db_session.delete(task)
    with pytest.raises(ValueError, match="immutable"):
        db_session.commit()
    db_session.rollback()

    assert db_session.query(GateRecord).filter(GateRecord.task_id == "V2-CASCADE").count() == 1


# ---------------------------------------------------------------------------
# Category 3: Four-Eyes Rule Enforcement Tests
# ---------------------------------------------------------------------------

def test_four_eyes_orm_validation_task():
    """Verify that setting executor == reviewer on Task raises FourEyesViolation in ORM layer."""
    # Instantiation with identical executor and reviewer
    with pytest.raises(FourEyesViolation) as exc_info:
        Task(id="V2-FE1", project="p", title="t", executor="@alice", reviewer="@alice")
    assert "Four-eyes violation" in str(exc_info.value)

    # Attribute mutation on Task
    task = Task(id="V2-FE2", project="p", title="t", executor="@alice", reviewer="@bob")
    assert task.executor == "@alice"
    assert task.reviewer == "@bob"

    with pytest.raises(FourEyesViolation):
        task.reviewer = "@alice"

    with pytest.raises(FourEyesViolation):
        task.executor = "@bob"


def test_four_eyes_orm_validation_gate_record():
    """Verify that setting executor == reviewer on GateRecord raises FourEyesViolation in ORM layer."""
    with pytest.raises(FourEyesViolation) as exc_info:
        GateRecord(task_id="V2-FE3", gate_type="verdict", executor="@alice", reviewer="@alice")
    assert "Four-eyes violation" in str(exc_info.value)

    gate = GateRecord(task_id="V2-FE4", gate_type="verdict", executor="@alice", reviewer="@bob")
    with pytest.raises(FourEyesViolation):
        gate.reviewer = "@alice"


def test_four_eyes_db_check_constraint_tasks(db_session):
    """Verify that raw SQL insert breaching Four-Eyes rule triggers DB IntegrityError CheckConstraint."""
    raw_sql = text("""
        INSERT INTO tasks (id, project, title, executor, reviewer, status, current_gate, mode)
        VALUES ('V2-DB-FE1', 'proj', 'title', '@charlie', '@charlie', 'todo', 'spec', 'supervised')
    """)
    with pytest.raises(IntegrityError):
        db_session.execute(raw_sql)
        db_session.commit()
    db_session.rollback()


def test_four_eyes_db_check_constraint_gate_records(db_session):
    """Verify that raw SQL insert into gate_records breaching Four-Eyes triggers DB IntegrityError CheckConstraint."""
    # First insert a valid parent task
    task = Task(id="V2-PARENT", project="p", title="t")
    db_session.add(task)
    db_session.commit()

    raw_sql = text("""
        INSERT INTO gate_records (task_id, gate_type, status, executor, reviewer)
        VALUES ('V2-PARENT', 'verdict', 'passed', '@dave', '@dave')
    """)
    with pytest.raises(IntegrityError):
        db_session.execute(raw_sql)
        db_session.commit()
    db_session.rollback()


def test_four_eyes_allowed_cases(db_session):
    """Verify that setting only executor or only reviewer or distinct values is permitted."""
    t1 = Task(id="V2-OK1", project="p", title="t", executor="@alice", reviewer=None)
    t2 = Task(id="V2-OK2", project="p", title="t", executor=None, reviewer="@bob")
    t3 = Task(id="V2-OK3", project="p", title="t", executor="@alice", reviewer="@bob")
    db_session.add_all([t1, t2, t3])
    db_session.commit()

    assert t1.executor == "@alice" and t1.reviewer is None
    assert t2.executor is None and t2.reviewer == "@bob"
    assert t3.executor == "@alice" and t3.reviewer == "@bob"


# ---------------------------------------------------------------------------
# Category 4: task_dependencies (CTV2-094)
# ---------------------------------------------------------------------------

def test_task_dependency_crud_and_relationship(db_session):
    """Verify CRUD and the Task.depends_on projection over task_dependencies."""
    upstream = Task(id="DAG-UP", project="p", title="Upstream")
    downstream = Task(id="DAG-DOWN", project="p", title="Downstream")
    db_session.add_all([upstream, downstream])
    db_session.commit()

    edge = TaskDependency(task_id="DAG-DOWN", depends_on_task_id="DAG-UP")
    db_session.add(edge)
    db_session.commit()

    db_session.refresh(downstream)
    assert downstream.depends_on == ["DAG-UP"]

    fetched = db_session.get(TaskDependency, ("DAG-DOWN", "DAG-UP"))
    assert fetched is not None


def test_task_dependency_rejects_self_reference(db_session):
    """The `task_id <> depends_on_task_id` check constraint blocks self-loops."""
    task = Task(id="DAG-SELF", project="p", title="Self")
    db_session.add(task)
    db_session.commit()

    db_session.add(TaskDependency(task_id="DAG-SELF", depends_on_task_id="DAG-SELF"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_task_dependency_cascades_on_task_deletion(db_session):
    """Deleting either side of an edge removes the task_dependencies row."""
    upstream = Task(id="DAG-CASC-UP", project="p", title="Upstream")
    downstream = Task(id="DAG-CASC-DOWN", project="p", title="Downstream")
    db_session.add_all([upstream, downstream])
    db_session.commit()
    db_session.add(TaskDependency(task_id="DAG-CASC-DOWN", depends_on_task_id="DAG-CASC-UP"))
    db_session.commit()

    db_session.delete(downstream)
    db_session.commit()

    assert db_session.query(TaskDependency).count() == 0
