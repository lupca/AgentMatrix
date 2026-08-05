"""A ``failed`` task must not be a black hole.

Two incidents on 2026-08-05 put good work permanently out of reach:

* CTV2-1382 -- the token brake fired one second *after* the executor's
  ``execution`` gate was approved and ``result_ref`` was written, flipping the
  task to ``failed``.  A commit with 830 passing tests was stranded on its
  branch: ``request_review`` wanted ``awaiting-review``, ``attach_result``
  wanted ``dispatched``, ``land_task`` wanted an approved verdict, and a newly
  requested run was cancelled on arrival with "Task reached terminal state:
  failed".

* CTV2-1388 -- the task filed to fix the above was escalated to ``failed`` by
  the orchestration driver because its plan critic had not finished yet, and
  died the same way.  The defect killed its own fix.

These tests pin both halves of the repair: the brake stops spending without
destroying a delivered result, and there is a way back when a task does end up
``failed``.
"""

import pytest

from app.db.models import Agent, Project, Task
from app.services.task_orchestration import (
    OrchestrationError,
    PrerequisiteError,
    TaskOrchestrationService,
)
from app.services.task_validators import BrakeDecision, TaskValidator


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Reopen task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def _fire_token_brake(db, task: Task) -> None:
    TaskValidator(db)._record_brake(
        task,
        BrakeDecision(
            False,
            "Task token limit reached: 56,011,067 >= 40,000,000 tokens",
            "token_limit",
        ),
    )


def test_budget_brake_leaves_a_delivered_result_reviewable(service, db_session):
    """The exact CTV2-1382 shape: result already delivered, then the brake."""

    task = _add_task(
        db_session,
        "BRAKE-001",
        status="awaiting-review",
        executor="@executor",
        result_ref="52e96501bc73..a6c25818e117",
        current_gate="review_order",
    )

    _fire_token_brake(db_session, task)
    db_session.refresh(task)

    assert task.status == "awaiting-review"
    # The brake still has to be visible and still has to stop further spend --
    # it just may not throw the finished work away.
    assert task.awaiting_approval is True
    assert "token limit" in (task.error or "")


def test_budget_brake_still_fails_a_task_with_nothing_delivered(service, db_session):
    """No result yet means nothing to protect: the old behaviour must stand."""

    task = _add_task(
        db_session,
        "BRAKE-002",
        status="dispatched",
        executor="@executor",
    )

    _fire_token_brake(db_session, task)
    db_session.refresh(task)

    assert task.status == "failed"


def test_reopen_sends_a_delivered_task_back_to_the_review_boundary(service, db_session):
    task = _add_task(
        db_session,
        "REOPEN-001",
        status="failed",
        executor="@executor",
        result_ref="abc123",
        error="Task token limit reached",
        awaiting_approval=True,
    )

    result = service.reopen_failed_task(task_id=task.id, actor="chat:test")

    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.error is None
    assert result.task.awaiting_approval is False
    # Append-only: reopening adds a record, it does not edit the escalation
    # that closed the task.
    assert result.gate_record.gate_type == "reopen"
    assert result.gate_record.status == "approved"


def test_reopen_sends_an_undelivered_task_back_to_todo(service, db_session):
    task = _add_task(
        db_session,
        "REOPEN-002",
        status="failed",
        error="generated plan has no current independent critic acceptance",
        awaiting_approval=True,
    )

    result = service.reopen_failed_task(task_id=task.id, actor="chat:test")

    assert result.task.status == "todo"
    assert result.task.current_gate == "dispatch"


@pytest.mark.asyncio
async def test_reopen_handler_returns_a_serializable_payload(service, db_session):
    """Cover the handler, not just the service.

    The first cut of these tests exercised ``reopen_failed_task`` directly and
    passed, while the MCP handler still read ``result.record`` (the field is
    ``gate_record``).  Every real call blew up with an internal_error *after*
    the transition had already committed -- the task moved, the caller was
    told it had not.  A green service-level test proved nothing about the path
    users actually take.
    """

    from app.services.command_router import CommandRouter

    _add_task(
        db_session,
        "REOPEN-005",
        status="failed",
        executor="@executor",
        result_ref="abc123",
    )

    response = await CommandRouter(db_session)._handle_reopen_task(
        "REOPEN-005", "test-session"
    )

    assert "error" not in response
    assert response["action"] == "reopened"
    assert response["status"] == "awaiting-review"
    assert isinstance(response["gate_record_id"], int)


def test_reopen_refuses_a_task_that_is_not_failed(service, db_session):
    task = _add_task(db_session, "REOPEN-003", status="dispatched", executor="@executor")

    with pytest.raises(PrerequisiteError, match="requires status 'failed'"):
        service.reopen_failed_task(task_id=task.id, actor="chat:test")


def test_reopen_does_not_hand_out_a_verdict(service, db_session):
    """Reopening restores the review boundary; it must never skip four-eyes."""

    task = _add_task(
        db_session,
        "REOPEN-004",
        status="failed",
        executor="@executor",
        result_ref="abc123",
    )

    service.reopen_failed_task(task_id=task.id, actor="chat:test")
    db_session.refresh(task)

    # Still needs an independent reviewer and an approved pass verdict before
    # anything can land -- reopen only undid the terminal status.
    assert task.status == "awaiting-review"
    assert task.landed_ref is None
    with pytest.raises(OrchestrationError):
        service.land_task(task_id=task.id, actor="chat:test")
