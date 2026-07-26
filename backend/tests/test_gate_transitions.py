import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Agent, GateRecord, Project, Task
from app.services.task_orchestration import (
    ModeViolationError,
    PrerequisiteError,
    TaskOrchestrationService,
    TransitionConflictError,
)


@pytest.fixture
def service(db_session):
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


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Transition task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def test_plan_only_blocks_dispatch_and_records_rejection(service, db_session):
    task = _add_task(db_session, "MODE-001", mode="plan-only")

    with pytest.raises(ModeViolationError):
        service.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="plan-only-dispatch",
        )

    record = db_session.query(GateRecord).one()
    assert record.status == "rejected"
    assert record.mode == "plan-only"
    assert task.status == "todo"


def test_executor_success_stops_at_awaiting_review(service, db_session):
    task = _add_task(
        db_session,
        "EXEC-001",
        status="dispatched",
        executor="@executor",
    )

    result = service.record_execution_success(
        task_id=task.id,
        result_ref="abc123",
        actor="@executor",
        idempotency_key="run-1:success",
        run_id="run-1",
    )

    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.completed_at is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "awaiting-review"}, "expected status"),
        ({"reviewer": None}, "reviewer is required"),
        ({"result_ref": None}, "result_ref is required"),
    ],
)
def test_verdict_rejects_missing_prerequisites(
    service,
    db_session,
    changes,
    message,
):
    values = {
        "status": "in-review",
        "executor": "@executor",
        "reviewer": "@reviewer",
        "result_ref": "abc123",
    }
    values.update(changes)
    task = _add_task(db_session, f"VER-{len(db_session.new)}{message[:2]}", **values)

    with pytest.raises((PrerequisiteError, TransitionConflictError), match=message):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor=task.reviewer or "@reviewer",
            idempotency_key=f"verdict:{task.id}",
        )


def test_passing_verdict_requires_complete_passing_ac_results(service, db_session):
    task = _add_task(
        db_session,
        "VER-AC",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
        acceptance_criteria=["AC 1", "AC 2"],
    )

    with pytest.raises(PrerequisiteError, match="incomplete"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key="verdict-incomplete",
        )
    with pytest.raises(PrerequisiteError, match="every acceptance criterion"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}, {"passed": False}],
            actor="@reviewer",
            idempotency_key="verdict-failed-ac",
        )


def test_only_passing_verdict_service_transition_reaches_done(
    service,
    db_session,
):
    task = _add_task(
        db_session,
        "VER-PASS",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
    )

    result = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="verdict-pass",
    )

    assert result.task.status == "done"
    assert result.task.completed_at is not None
    assert result.gate_record.status == "approved"
    assert result.gate_record.output_ref == "pass"


def test_database_rejects_done_without_completion_fields(db_session):
    task = Task(
        id="INVALID-DONE",
        project="project",
        title="Invalid done task",
        status="done",
    )
    db_session.add(task)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
