import pytest
from sqlalchemy.exc import StatementError

from app.db.models import Agent, AgentRun, GateRecord, Project, Task
from app.services.task_orchestration import (
    IdempotencyConflictError,
    TaskOrchestrationService,
)


@pytest.fixture
def orchestration(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@executor",
            name="Executor",
            role="executor",
            cli="codex",
        )
    )
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _task(db, task_id: str, *, mode: str = "supervised") -> Task:
    task = Task(
        id=task_id,
        project="project",
        title="Governed task",
        mode=mode,
        acceptance_criteria=["Tests pass"],
    )
    db.add(task)
    db.commit()
    return task


def test_supervised_dispatch_commits_pending_and_resumes_separately(
    orchestration,
    db_session,
):
    task = _task(db_session, "GATE-001")

    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-1",
    )

    assert pending.status == "pending"
    assert pending.applied is False
    assert pending.agent_run is None
    assert pending.task.status == "todo"
    assert pending.task.awaiting_approval is True
    assert db_session.query(AgentRun).count() == 0

    approved = orchestration.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="dispatch-1:approval",
    )

    assert approved.status == "approved"
    assert approved.applied is True
    assert approved.task.status == "dispatched"
    assert approved.task.executor == "@executor"
    assert approved.agent_run is not None
    assert approved.gate_record.parent_id == pending.gate_record.id


def test_bypass_dispatch_is_audited_and_idempotent(orchestration, db_session):
    task = _task(db_session, "GATE-002", mode="bypass")
    request = dict(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-bypass",
    )

    first = orchestration.request_dispatch(**request)
    second = orchestration.request_dispatch(**request)

    assert first.status == "approved"
    assert first.agent_run.id == second.agent_run.id
    assert db_session.query(AgentRun).count() == 1
    assert (
        db_session.query(GateRecord)
        .filter(GateRecord.idempotency_key == "dispatch-bypass")
        .count()
        == 1
    )


def test_idempotency_key_cannot_be_reused_with_new_input(
    orchestration,
    db_session,
):
    task = _task(db_session, "GATE-003")
    orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="same-key",
    )

    with pytest.raises(IdempotencyConflictError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="same-key",
            timeout_seconds=60,
        )


def test_gate_records_are_append_only(orchestration, db_session):
    task = _task(db_session, "GATE-004")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="immutable",
    )

    pending.gate_record.status = "approved"
    with pytest.raises((ValueError, StatementError), match="append-only"):
        db_session.commit()
    db_session.rollback()


def test_decide_gate_idempotent_retry_preserves_dispatch_context(orchestration, db_session):
    task = _task(db_session, "GATE-005")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-5",
    )

    decide_args = dict(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="dispatch-5:approval",
    )

    first = orchestration.decide_gate(**decide_args)
    second = orchestration.decide_gate(**decide_args)

    assert first.status == "approved"
    assert second.status == "approved"
    assert first.agent_run.id == second.agent_run.id
    assert second.context is not None
    assert second.context.get("repo_root") == "/tmp"


