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
    assert spec.parameters["properties"]["option"]["enum"] == ["request_review"]
    assert spec.required_role == "executor"


def test_attach_result_option_done_is_rejected(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(
        id="TASK-100", project="proj", title="Test task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(OrchestrationError, match="cannot mark a task done"):
        orch_service.attach_result(task_id="TASK-100", commit=commit, option="done")

    db_session.refresh(task)
    assert task.status == "dispatched"
    assert db_session.query(GateRecord).filter_by(task_id=task.id).count() == 0


def test_attach_result_from_in_review_is_rejected(db_session, service):
    orch_service, _repo_root, commit = service
    task = Task(
        id="TASK-1363", project="proj", title="Exploit regression",
        status="in-review", executor="@executor", reviewer="@reviewer",
        result_ref="old-base..old-head",
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(TransitionConflictError, match="expected status 'dispatched'"):
        orch_service.attach_result(task_id=task.id, commit=commit)

    db_session.refresh(task)
    assert task.status == "in-review"
    assert task.final_verdict is None


def test_attach_result_option_request_review(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(
        id="TASK-101", project="proj", title="Review task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    res = orch_service.attach_result(task_id="TASK-101", commit=commit, option="request_review")
    assert res.applied is True
    assert res.task.status == "awaiting-review"
    assert res.task.current_gate == "review_order"
    assert res.task.result_ref.endswith(commit[:12])
    assert ".." in res.task.result_ref
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


def test_attach_result_does_not_wake_dependents_before_review(db_session, service):
    orch_service, repo_root, commit = service
    t1 = Task(
        id="TASK-102", project="proj", title="Parent",
        status="dispatched", executor="@executor",
    )
    t2 = Task(id="TASK-103", project="proj", title="Child", status="todo")
    dep = TaskDependency(task_id="TASK-103", depends_on_task_id="TASK-102")
    db_session.add_all([t1, t2, dep])
    db_session.commit()

    res = orch_service.attach_result(task_id="TASK-102", commit=commit)
    assert res.task.status == "awaiting-review"
    assert [t.id for t in orch_service.unmet_dependencies("TASK-103")] == ["TASK-102"]


def test_attach_result_command_router_execute_tool(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(
        id="TASK-104", project="proj", title="Router task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    router = CommandRouter(db_session)
    res = pytest.strip_result if False else None  # avoid lint unused
    import asyncio
    out = asyncio.run(
        router.execute_tool(
            "attach_result",
            {"task_id": "TASK-104", "commit": commit},
            "session-1",
        )
    )
    assert out["action"] == "result_attached"
    assert out["status"] == "awaiting-review"
    assert out["commit"].endswith(commit[:12])


def test_attach_result_slash_command(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(
        id="TASK-105", project="proj", title="Slash task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    router = CommandRouter(db_session)
    cmd, args = router.parse(f"/attach-result TASK-105 {commit} request_review")
    assert cmd == "attach_result"

    import asyncio
    out = asyncio.run(router.execute(cmd, args, "session-1"))
    assert out["action"] == "result_attached"
    assert out["status"] == "awaiting-review"
    assert out["commit"].endswith(commit[:12])


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
    task2 = Task(
        id="TASK-107", project="proj", title="Task 107",
        status="dispatched", executor="@executor",
    )
    db_session.add(task2)
    db_session.commit()
    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="TASK-107", commit=commit, option="invalid")

    # Non-existent commit error
    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="TASK-107", commit="0000000000000000000000000000000000000000")


def test_attach_result_idempotency(db_session, service):
    orch_service, repo_root, commit = service
    task = Task(
        id="TASK-108", project="proj", title="Idempotent task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    res1 = orch_service.attach_result(
        task_id="TASK-108", commit=commit, idempotency_key="key-108"
    )
    res2 = orch_service.attach_result(
        task_id="TASK-108", commit=commit, idempotency_key="key-108"
    )

    assert res1.gate_record.id == res2.gate_record.id
    records = db_session.query(GateRecord).filter(GateRecord.task_id == "TASK-108").all()
    assert len(records) == 1


def _second_commit(repo_root: str) -> str:
    """Add a real second commit so the repo has a non-root HEAD."""
    (os.path.join(repo_root, "feature.txt"))
    with open(os.path.join(repo_root, "feature.txt"), "w") as fh:
        fh.write("feature\n")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-m", "feature"], cwd=repo_root, check=True, capture_output=True)
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


def test_attach_result_stores_base_head_range(db_session, service):
    """CTV2-1337: a bare hash must be normalised to '<base>..<head>'.

    Storing a bare hash left request_review permanently blocked, because it
    refuses anything that is not a committed base..head range.
    """
    orch_service, repo_root, _root = service
    head = _second_commit(repo_root)

    task = Task(
        id="TASK-1337", project="proj", title="Range task",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    orch_service.attach_result(task_id="TASK-1337", commit=head)

    db_session.refresh(task)
    assert ".." in task.result_ref, f"expected a range, got {task.result_ref!r}"
    base_part, _, head_part = task.result_ref.partition("..")
    assert head.startswith(head_part)
    assert base_part and base_part != head_part


def test_attach_result_accepts_explicit_range(db_session, service):
    orch_service, repo_root, root = service
    head = _second_commit(repo_root)

    task = Task(
        id="TASK-1337B", project="proj", title="Explicit range",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    orch_service.attach_result(
        task_id="TASK-1337B", commit=f"{root}..{head}"
    )

    db_session.refresh(task)
    base_part, _, head_part = task.result_ref.partition("..")
    assert root.startswith(base_part)
    assert head.startswith(head_part)


def test_attach_result_root_commit_uses_empty_tree_base(db_session, service):
    """A root commit has no parent; the range must still be well formed."""
    orch_service, repo_root, root = service

    task = Task(
        id="TASK-1337C", project="proj", title="Root commit",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    orch_service.attach_result(task_id="TASK-1337C", commit=root)

    db_session.refresh(task)
    assert ".." in task.result_ref
    base_part, _, head_part = task.result_ref.partition("..")
    assert root.startswith(head_part)
    assert base_part and base_part != head_part


def test_attach_result_rejects_malformed_range(db_session, service):
    orch_service, repo_root, root = service

    task = Task(
        id="TASK-1337D", project="proj", title="Bad range",
        status="dispatched", executor="@executor",
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(OrchestrationError):
        orch_service.attach_result(task_id="TASK-1337D", commit=f"{root}..")
