import os
import time
import pytest
from datetime import date, datetime
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
from alembic.config import Config
from alembic import command

from app.db.base import Base
from app.db.models import Task, GateRecord, Session, AuditLog, Project, Agent
from app.graph.state import FourEyesViolation


# ---------------------------------------------------------------------------
# Stress Test 1: High-Volume Model Instantiation & Bulk Insertion
# ---------------------------------------------------------------------------

def test_high_volume_model_instantiation():
    """Stress test memory and CPU for instantiating 10,000 Task and GateRecord instances."""
    start_time = time.time()

    tasks = [
        Task(
            id=f"STRESS-{i:05d}",
            project="control-tower-v2",
            title=f"Stress Task {i}",
            raw_input=f"Raw input specification for task {i}",
            status="todo",
            current_gate="spec",
            mode="supervised",
            priority="high",
            risk="low",
            executor=f"@builder-{i}",
            reviewer=f"@checker-{i}",
            acceptance_criteria=[f"AC-{j}" for j in range(5)],
            files=[f"file_{j}.py" for j in range(3)],
            tests=[f"test_{j}.py" for j in range(3)],
        )
        for i in range(10000)
    ]

    elapsed = time.time() - start_time
    assert len(tasks) == 10000
    assert elapsed < 2.0, f"Instantiation took too long: {elapsed:.2f}s"


def test_bulk_db_insertions_and_retrieval(db_session):
    """Stress test inserting 1,000 Tasks and 3,000 GateRecords into DB and query performance."""
    start_time = time.time()

    # Bulk insert 1,000 tasks
    tasks = [
        Task(
            id=f"BULK-{i:04d}",
            project="bulk-project",
            title=f"Bulk Task {i}",
            status="in_progress" if i % 2 == 0 else "todo",
            current_gate="plan",
            executor=f"@dev-{i}",
            reviewer=f"@rev-{i}",
        )
        for i in range(1000)
    ]
    db_session.add_all(tasks)
    db_session.commit()

    # Bulk insert 3,000 GateRecords linked to those tasks
    gate_records = []
    for i in range(1000):
        for g_type in ["spec", "plan", "dispatch"]:
            gate_records.append(
                GateRecord(
                    task_id=f"BULK-{i:04d}",
                    gate_type=g_type,
                    status="approved",
                    executor=f"@dev-{i}",
                    reviewer=f"@rev-{i}",
                    input_payload={"step": g_type, "index": i},
                    output_payload={"result": "ok"},
                )
            )
    db_session.add_all(gate_records)
    db_session.commit()

    total_insert_time = time.time() - start_time

    # Query counts & verify integrity
    task_count = db_session.query(Task).filter(Task.project == "bulk-project").count()
    gate_count = db_session.query(GateRecord).count()

    assert task_count == 1000
    assert gate_count == 3000
    assert total_insert_time < 5.0, f"Bulk insertion took too long: {total_insert_time:.2f}s"


def test_large_json_payload_handling(db_session):
    """Stress test storing and retrieving large JSON payloads in Task and GateRecord models."""
    large_list = [f"item_{i}" * 50 for i in range(1000)]  # ~350KB payload
    large_dict = {f"key_{i}": "val_" * 100 for i in range(500)}  # ~250KB payload

    task = Task(
        id="LARGE-JSON-01",
        project="json-stress",
        title="Task with large JSON payload",
        acceptance_criteria=large_list,
        findings=[large_dict],
        prediction_factors=large_dict,
    )
    db_session.add(task)

    gate = GateRecord(
        task_id="LARGE-JSON-01",
        gate_type="review",
        status="approved",
        input_payload=large_dict,
        output_payload={"list": large_list},
    )
    db_session.add(gate)
    db_session.commit()

    # Retrieve and verify exact match
    fetched_task = db_session.query(Task).filter(Task.id == "LARGE-JSON-01").first()
    fetched_gate = db_session.query(GateRecord).filter(GateRecord.task_id == "LARGE-JSON-01").first()

    assert len(fetched_task.acceptance_criteria) == 1000
    assert len(fetched_task.findings[0]) == 500
    assert len(fetched_gate.input_payload) == 500
    assert len(fetched_gate.output_payload["list"]) == 1000


def test_heavy_cascade_deletion_is_blocked_by_immutable_ledger(db_session):
    """A task with ledger evidence cannot erase that evidence by cascading."""
    tasks = []
    for i in range(200):
        t_id = f"CAS-{i:03d}"
        task = Task(id=t_id, project="cascade-proj", title=f"Cascade task {i}")
        db_session.add(task)
        tasks.append(task)

        for g in range(5):
            db_session.add(GateRecord(task_id=t_id, gate_type=f"gate_{g}", status="pending"))
        for s in range(2):
            db_session.add(
                Session(
                    task_id=t_id,
                    project_id="cascade-proj",
                    context_level="task",
                    thread_id=f"t-{t_id}-{s}",
                )
            )

    db_session.commit()

    assert db_session.query(Task).filter(Task.project == "cascade-proj").count() == 200
    assert db_session.query(GateRecord).count() == 1000
    assert db_session.query(Session).count() == 400

    db_session.delete(tasks[0])
    with pytest.raises(ValueError, match="immutable"):
        db_session.commit()
    db_session.rollback()

    assert db_session.query(Task).filter(Task.project == "cascade-proj").count() == 200
    assert db_session.query(GateRecord).count() == 1000


# ---------------------------------------------------------------------------
# Stress Test 2: Four-Eyes Rule Boundary Cases & Constraint Behavior
# ---------------------------------------------------------------------------

def test_four_eyes_null_combinations(db_session):
    """Boundary Case: Test all NULL executor vs reviewer combinations."""
    # 1. executor=None, reviewer=None
    t_null_both = Task(id="FE-NULL-1", project="p", title="t", executor=None, reviewer=None)
    g_null_both = GateRecord(task_id="FE-NULL-1", gate_type="spec", executor=None, reviewer=None)
    db_session.add_all([t_null_both, g_null_both])
    db_session.commit()

    # 2. executor="alice", reviewer=None
    t_exec_only = Task(id="FE-NULL-2", project="p", title="t", executor="alice", reviewer=None)
    g_exec_only = GateRecord(task_id="FE-NULL-2", gate_type="spec", executor="alice", reviewer=None)
    db_session.add_all([t_exec_only, g_exec_only])
    db_session.commit()

    # 3. executor=None, reviewer="bob"
    t_rev_only = Task(id="FE-NULL-3", project="p", title="t", executor=None, reviewer="bob")
    g_rev_only = GateRecord(task_id="FE-NULL-3", gate_type="spec", executor=None, reviewer="bob")
    db_session.add_all([t_rev_only, g_rev_only])
    db_session.commit()

    assert db_session.query(Task).count() == 3
    assert db_session.query(GateRecord).count() == 3


def test_four_eyes_same_user_instantiation_order(db_session):
    """Boundary Case: Test executor == reviewer in Task and GateRecord regardless of kwarg/attribute set order."""
    # Order 1: executor then reviewer
    with pytest.raises(FourEyesViolation):
        Task(id="FE-ORD-1", project="p", title="t", executor="same_user", reviewer="same_user")

    # Order 2: reviewer then executor on instantiated object
    t = Task(id="FE-ORD-2", project="p", title="t", reviewer="user_a", executor=None)
    with pytest.raises(FourEyesViolation):
        t.executor = "user_a"

    # Order 3: executor then reviewer on GateRecord
    with pytest.raises(FourEyesViolation):
        GateRecord(task_id="FE-ORD-1", gate_type="plan", executor="user_x", reviewer="user_x")

    # Order 4: reviewer then executor on GateRecord
    g = GateRecord(task_id="FE-ORD-1", gate_type="plan", executor=None, reviewer="user_x")
    with pytest.raises(FourEyesViolation):
        g.executor = "user_x"


def test_four_eyes_case_sensitivity_and_whitespace_edge_cases(db_session):
    """Principal comparison normalizes case and surrounding whitespace."""
    with pytest.raises(FourEyesViolation):
        Task(id="FE-EDGE-1", project="p", title="t", executor="Alice", reviewer="alice")
    with pytest.raises(FourEyesViolation):
        Task(id="FE-EDGE-2", project="p", title="t", executor="alice ", reviewer="alice")


def test_four_eyes_bulk_update_bypasses_orm_but_caught_by_db(db_session):
    """Stress Case: Bulk ORM update bypassing ORM @validates is caught by DB CheckConstraint upon query execution."""
    # Create valid task
    t = Task(id="FE-BULK-1", project="p", title="t", executor="alice", reviewer="bob")
    db_session.add(t)
    db_session.commit()

    # Bulk update setting reviewer="alice" (matches executor="alice")
    # ORM @validates does NOT run on query.update(), but DB CheckConstraint triggers IntegrityError immediately
    with pytest.raises(IntegrityError):
        db_session.query(Task).filter(Task.id == "FE-BULK-1").update({"reviewer": "alice"})
    db_session.rollback()


def test_four_eyes_raw_sql_update_and_insert_constraints(db_session):
    """Stress Case: Raw SQL INSERT/UPDATE breaching Four-Eyes in tasks and gate_records tables."""
    # Valid parent task
    db_session.add(Task(id="FE-RAW-PARENT", project="p", title="t", executor="alice", reviewer="bob"))
    db_session.commit()

    # 1. Raw SQL UPDATE tasks breaching constraint
    sql_update_task = text("UPDATE tasks SET reviewer = 'alice' WHERE id = 'FE-RAW-PARENT'")
    with pytest.raises(IntegrityError):
        db_session.execute(sql_update_task)
        db_session.commit()
    db_session.rollback()

    # 2. Raw SQL INSERT gate_records breaching constraint
    sql_insert_gate = text("""
        INSERT INTO gate_records (task_id, gate_type, status, executor, reviewer)
        VALUES ('FE-RAW-PARENT', 'verdict', 'passed', 'charlie', 'charlie')
    """)
    with pytest.raises(IntegrityError):
        db_session.execute(sql_insert_gate)
        db_session.commit()
    db_session.rollback()

    # 3. Raw SQL UPDATE gate_records breaching constraint
    db_session.add(GateRecord(task_id="FE-RAW-PARENT", gate_type="verdict", executor="charlie", reviewer="dave"))
    db_session.commit()
    sql_update_gate = text("UPDATE gate_records SET reviewer = 'charlie' WHERE task_id = 'FE-RAW-PARENT'")
    with pytest.raises(IntegrityError):
        db_session.execute(sql_update_gate)
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Stress Test 3: Migration Upgrade & Downgrade Cycle
# ---------------------------------------------------------------------------

def test_alembic_migration_upgrade_downgrade_cycle():
    """Stress test Alembic migration 001_initial: upgrade -> downgrade -> upgrade."""
    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    connection = engine.connect()

    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ini_path = os.path.join(backend_dir, "alembic.ini")
    alembic_cfg = Config(ini_path)
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    alembic_cfg.attributes["connection"] = connection

    # 1. Upgrade to head
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(connection)
    assert "tasks" in inspector.get_table_names()
    assert "gate_records" in inspector.get_table_names()

    # 2. Downgrade to base
    command.downgrade(alembic_cfg, "base")
    inspector_down = inspect(connection)
    down_tables = inspector_down.get_table_names()
    assert "tasks" not in down_tables
    assert "gate_records" not in down_tables
    assert "sessions" not in down_tables
    assert "audit_log" not in down_tables

    # 3. Re-upgrade to head
    command.upgrade(alembic_cfg, "head")
    inspector_reup = inspect(connection)
    reup_tables = inspector_reup.get_table_names()
    assert "tasks" in reup_tables
    assert "gate_records" in reup_tables
    assert "sessions" in reup_tables
    assert "audit_log" in reup_tables

    connection.close()
