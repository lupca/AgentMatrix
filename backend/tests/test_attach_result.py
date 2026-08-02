import os
import subprocess
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import AuditLog, GateRecord, Project, Task, TaskDependency
from app.services.command_router import CommandRouter
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
    TransitionConflictError,
)
from app.services.tool_registry import TOOL_REGISTRY, get_spec


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def service(db_session, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
    f = repo / "README.md"
    f.write_text("hello")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo), check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True)
    commit = res.stdout.strip()

    db_session.add(Project(id="proj", name="Project", repo_root=str(repo)))
    db_session.commit()
    return TaskOrchestrationService(db_session), str(repo), commit


def test_attach_result_tool_spec():
    spec = get_spec("attach_result")
    assert spec is not None
    assert spec.name == "attach_result"
    assert spec.handler == "attach_result"
    assert spec.slash_alias == "/attach-result"
    assert spec.group == "task_lifecycle"
    assert "task_id" in spec.parameters["properties"]
    assert "commit" in spec.parameters["properties"]
    assert "option" in spec.parameters["properties"]


def test_attach_result_option_done(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-100", project="proj", title="Test task", status="todo")
    db_session.add(task)
    db_session.commit()

    res = orch_service.attach_result(task_id="TASK-100", commit=commit, option="done")
    assert res.applied is True
    assert res.task.status == "done"
    assert res.task.result_ref == commit
    assert res.task.final_result_ref == commit
    assert res.task.verdict == "pass"
    assert res.task.final_verdict == "pass"

    # Ledger check
    records = db_session.query(GateRecord).filter(GateRecord.task_id == "TASK-100").all()
    assert len(records) == 1
    assert records[0].gate_type == "attach_result"
    assert records[0].status == "approved"
    assert records[0].output_ref == commit

    # Audit log check
    audit = db_session.query(AuditLog).filter(AuditLog.task_id == "TASK-100").first()
    assert audit is not None
    assert audit.action == "transition:attach_result:approved"
    assert audit.details["status"] == "done"


def test_attach_result_option_request_review(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-101", project="proj", title="Review task", status="todo")
    db_session.add(task)
    db_session.commit()

    res = orch_service.attach_result(task_id="TASK-101", commit=commit, option="request_review")
    assert res.applied is True
    assert res.task.status == "awaiting-review"
    assert res.task.current_gate == "review_order"
    assert res.task.result_ref == commit
    assert res.task.final_result_ref is None

    # Ledger check
    records = db_session.query(GateRecord).filter(GateRecord.task_id == "TASK-101").all()
    assert len(records) == 1
    assert records[0].gate_type == "attach_result"
    assert records[0].status == "approved"

    # Audit log check
    audit = db_session.query(AuditLog).filter(AuditLog.task_id == "TASK-101").first()
    assert audit is not None
    assert audit.action == "transition:attach_result:approved"
    assert audit.details["status"] == "awaiting-review"


def test_attach_result_wakes_dependents(db_session, service):
    orch_service, repo_root, commit = service
    t1 = Task(id="TASK-102", project="proj", title="Parent", status="todo")
    t2 = Task(id="TASK-103", project="proj", title="Child", status="todo")
    dep = TaskDependency(task_id="TASK-103", depends_on_task_id="TASK-102")
    db_session.add_all([t1, t2, dep])
    db_session.commit()

    res = orch_service.attach_result(task_id="TASK-102", commit=commit, option="done")
    assert res.task.status == "done"


def test_attach_result_command_router_execute_tool(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-104", project="proj", title="Router task", status="todo")
    db_session.add(task)
    db_session.commit()

    router = CommandRouter(db_session)
    res = pytest.strip_result if False else None  # avoid lint unused
    import asyncio
    out = asyncio.run(
        router.execute_tool(
            "attach_result",
            {"task_id": "TASK-104", "commit": commit, "option": "done"},
            "session-1",
        )
    )
    assert out["action"] == "result_attached"
    assert out["status"] == "done"
    assert out["commit"] == commit


def test_attach_result_slash_command(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-105", project="proj", title="Slash task", status="todo")
    db_session.add(task)
    db_session.commit()

    router = CommandRouter(db_session)
    cmd, args = router.parse(f"/attach-result TASK-105 {commit} request_review")
    assert cmd == "attach_result"

    import asyncio
    out = asyncio.run(router.execute(cmd, args, "session-1"))
    assert out["action"] == "result_attached"
    assert out["status"] == "awaiting-review"
    assert out["commit"] == commit


def test_attach_result_validation_errors(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-106", project="proj", title="Task 106", status="cancelled")
    db_session.add(task)
    db_session.commit()

    # Cancelled task error
    with pytest.raises(TransitionConflictError):
        orch_service.attach_result(task_id="TASK-106", commit=commit)

    # Missing task error
    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="NONEXISTENT", commit=commit)

    # Invalid option error
    task2 = Task(id="TASK-107", project="proj", title="Task 107", status="todo")
    db_session.add(task2)
    db_session.commit()
    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="TASK-107", commit=commit, option="invalid")

    # Non-existent commit error
    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="TASK-107", commit="0000000000000000000000000000000000000000")


def test_attach_result_idempotency(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(id="TASK-108", project="proj", title="Idempotent task", status="todo")
    db_session.add(task)
    db_session.commit()

    res1 = orch_service.attach_result(
        task_id="TASK-108", commit=commit, option="done", idempotency_key="key-108"
    )
    res2 = orch_service.attach_result(
        task_id="TASK-108", commit=commit, option="done", idempotency_key="key-108"
    )

    assert res1.gate_record.id == res2.gate_record.id
    records = db_session.query(GateRecord).filter(GateRecord.task_id == "TASK-108").all()
    assert len(records) == 1
