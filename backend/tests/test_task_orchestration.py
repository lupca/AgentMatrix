import pytest
from sqlalchemy.exc import StatementError

from app.db.models import Agent, AgentRun, GateRecord, Project, Task
from app.services.task_orchestration import (
    IdempotencyConflictError,
    StaleIdempotencyRecordError,
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


def test_reusing_idempotency_key_after_run_goes_terminal_is_rejected(
    orchestration,
    db_session,
):
    """Regression for CTV2-088: a stale dispatch record must never be
    replayed as `applied=True` once its AgentRun has left queued/running,
    even though the task's own status has since reverted to a state that
    would otherwise pass `_assert_status` (e.g. after a queue failure)."""
    task = _task(db_session, "GATE-STALE", mode="bypass")

    first = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="stale-key",
    )
    assert first.applied is True
    run = first.agent_run
    assert run is not None

    # Simulate the run exhausting all its retries (e.g. the queue failed to
    # accept it) and the task being reset to "todo" so it once again
    # satisfies expected_status. `attempt >= max_attempts` is what makes a
    # "failed" run actually terminal (round 2 / AC3) — the worker retries a
    # failed run in place otherwise, so it would still be in flight.
    run.status = "failed"
    run.attempt = run.max_attempts
    task.status = "todo"
    db_session.commit()

    with pytest.raises(StaleIdempotencyRecordError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="stale-key",
        )

    # No new run was silently created, and the dead run is still the only one.
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


def test_reusing_idempotency_key_after_failed_run_with_retries_left_is_not_stale(
    orchestration,
    db_session,
):
    """AC3 nuance: a "failed" run that hasn't exhausted `max_attempts` is not
    terminal — the worker still retries it in place — so the cached record
    must keep being replayed as `applied=True`, not rejected as stale."""
    task = _task(db_session, "GATE-RETRYABLE", mode="bypass")

    first = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="retryable-key",
    )
    run = first.agent_run
    run.status = "failed"
    run.attempt = 1
    run.max_attempts = 3
    db_session.commit()

    replay = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="retryable-key",
    )
    assert replay.applied is True
    assert replay.agent_run.id == run.id
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


@pytest.mark.parametrize("terminal_status", ["success", "timeout", "cancelled"])
def test_reusing_idempotency_key_after_run_completes_naturally_is_rejected(
    orchestration,
    db_session,
    terminal_status,
):
    """Regression for CTV2-088 round 2 (missing coverage flagged by
    reviewer): the only stale-record test before this covered a queue-failure
    style "failed" run. A dispatch idempotency key reused after the run
    finished on its own — success, timeout, or cancellation — must be
    rejected the same way, never silently replayed as `applied=True`."""
    task = _task(db_session, f"GATE-STALE-{terminal_status.upper()}", mode="bypass")

    first = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="stale-natural-key",
    )
    run = first.agent_run
    run.status = terminal_status
    # Whatever status the task ended up in, put it back to "todo" so only
    # the run-staleness guard (not `_assert_status`) is what would reject a
    # naive replay here.
    task.status = "todo"
    db_session.commit()

    with pytest.raises(StaleIdempotencyRecordError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="stale-natural-key",
        )

    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


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


@pytest.mark.parametrize("terminal_status", ["success", "failed", "timeout", "cancelled"])
def test_decide_gate_replay_after_run_goes_terminal_still_returns_cached_decision(
    orchestration,
    db_session,
    terminal_status,
):
    """Regression for CTV2-088 round 2: round 1 applied the dispatch
    stale-record guard to `decide_gate` too, which made replaying an
    already-decided gate (e.g. a duplicate POST /gates/{id}/decision) raise
    `StaleIdempotencyRecordError` forever once the run it created finished —
    even a perfectly normal success. The approve decision is an immutable
    historical fact; its replay must keep returning applied=True with the
    original run, regardless of that run's later status."""
    task = _task(db_session, f"GATE-APPROVE-REPLAY-{terminal_status.upper()}")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=f"dispatch-{terminal_status}",
    )

    decide_args = dict(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key=f"dispatch-{terminal_status}:approval",
    )
    first = orchestration.decide_gate(**decide_args)
    run = first.agent_run
    assert run is not None

    run.status = terminal_status
    if terminal_status == "failed":
        run.attempt = run.max_attempts
    db_session.commit()

    replay = orchestration.decide_gate(**decide_args)
    assert replay.applied is True
    assert replay.status == "approved"
    assert replay.agent_run.id == run.id


