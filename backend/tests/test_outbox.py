from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db.models import Agent, AgentRun, OutboxEvent, Project, Task
from app.services.outbox import (
    MAX_PUBLISH_ATTEMPTS,
    publish_pending_events,
    reconcile_orphaned_runs,
)


@pytest.fixture
def seeded(db_session):
    db_session.add(Project(id="proj-outbox", name="Outbox", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.add(
        Task(
            id="OUTBOX-T1",
            project="proj-outbox",
            title="Task",
            status="dispatched",
            acceptance_criteria=["Tests pass"],
            executor="@executor",
        )
    )
    db_session.commit()
    return db_session


def _run(db, run_id="run-1", **overrides):
    defaults = dict(
        id=run_id,
        task_id="OUTBOX-T1",
        agent_id="@executor",
        cli="codex",
        command="codex exec",
        status="queued",
    )
    defaults.update(overrides)
    run = AgentRun(**defaults)
    db.add(run)
    db.commit()
    return run


def _event(db, run, **overrides):
    defaults = dict(
        event_type="run_requested",
        payload={
            "run_id": run.id,
            "task_id": run.task_id,
            "command": run.command,
            "repo_root": "/tmp",
            "timeout_seconds": run.timeout_seconds,
        },
    )
    defaults.update(overrides)
    event = OutboxEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_publish_pending_events_enqueues_and_marks_published(seeded):
    run = _run(seeded)
    event = _event(seeded, run)

    fake_message = MagicMock(message_id="msg-123")
    with patch("app.workers.agent_runner.run_agent.send", return_value=fake_message) as mock_send:
        counts = publish_pending_events(seeded)

    mock_send.assert_called_once_with(run.id, run.task_id, run.command, "/tmp", run.timeout_seconds)
    assert counts == {"published": 1, "deferred": 0, "dead_lettered": 0}
    seeded.refresh(event)
    seeded.refresh(run)
    assert event.published_at is not None
    assert event.attempts == 1
    assert run.dramatiq_message_id == "msg-123"


def test_publish_pending_events_skips_run_already_enqueued_by_fast_path(seeded):
    """A run whose synchronous run_agent.send() already succeeded (i.e. it
    already has a dramatiq_message_id) must not be sent a second time."""
    run = _run(seeded, dramatiq_message_id="already-sent")
    event = _event(seeded, run)

    with patch("app.workers.agent_runner.run_agent.send") as mock_send:
        counts = publish_pending_events(seeded)

    mock_send.assert_not_called()
    assert counts == {"published": 1, "deferred": 0, "dead_lettered": 0}
    seeded.refresh(event)
    assert event.published_at is not None
    assert event.attempts == 0


def test_publish_pending_events_ignores_run_no_longer_actionable(seeded):
    run = _run(seeded, status="failed")
    event = _event(seeded, run)

    with patch("app.workers.agent_runner.run_agent.send") as mock_send:
        counts = publish_pending_events(seeded)

    mock_send.assert_not_called()
    seeded.refresh(event)
    assert event.published_at is not None


def test_publish_failure_increments_attempts_and_defers_via_backoff(seeded):
    run = _run(seeded)
    event = _event(seeded, run)

    with patch("app.workers.agent_runner.run_agent.send", side_effect=RuntimeError("broker down")):
        counts = publish_pending_events(seeded)

    seeded.refresh(event)
    assert counts == {"published": 0, "deferred": 1, "dead_lettered": 0}
    assert event.attempts == 1
    assert event.dead_letter is False
    assert event.last_error == "broker down"
    assert event.last_attempted_at is not None

    # Immediately polling again should defer: backoff has not elapsed yet.
    with patch("app.workers.agent_runner.run_agent.send") as mock_send:
        counts = publish_pending_events(seeded)
    mock_send.assert_not_called()
    assert counts == {"published": 0, "deferred": 1, "dead_lettered": 0}


def test_publish_dead_letters_after_max_attempts_and_fails_the_run(seeded):
    run = _run(seeded)
    event = _event(seeded, run, attempts=MAX_PUBLISH_ATTEMPTS - 1)

    with patch("app.workers.agent_runner.run_agent.send", side_effect=RuntimeError("still down")):
        counts = publish_pending_events(seeded)

    seeded.refresh(event)
    seeded.refresh(run)
    assert counts == {"published": 0, "deferred": 0, "dead_lettered": 1}
    assert event.dead_letter is True
    assert event.attempts == MAX_PUBLISH_ATTEMPTS
    assert run.status == "failed"
    assert run.error_message is not None


def test_publish_pending_events_is_idempotent_across_repeated_polls(seeded):
    """Simulates the crash-recovery scenario the outbox exists for: a
    publish attempt that already succeeded must never be repeated on the
    next poll tick."""
    run = _run(seeded)
    event = _event(seeded, run)

    fake_message = MagicMock(message_id="msg-1")
    with patch("app.workers.agent_runner.run_agent.send", return_value=fake_message) as mock_send:
        publish_pending_events(seeded)
        publish_pending_events(seeded)

    assert mock_send.call_count == 1


def test_reconcile_orphaned_runs_recreates_missing_outbox_event(seeded):
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    run = _run(seeded, queued_at=old)
    # No OutboxEvent for this run at all -- simulates a lost/never-written row.

    reconciled = reconcile_orphaned_runs(seeded, older_than_seconds=60)

    assert reconciled == 1
    events = [
        e for e in seeded.query(OutboxEvent).all() if (e.payload or {}).get("run_id") == run.id
    ]
    assert len(events) == 1
    assert events[0].payload["repo_root"] == "/tmp"
    assert events[0].published_at is None


def test_reconcile_orphaned_runs_skips_runs_already_tracked(seeded):
    old = datetime.now(timezone.utc) - timedelta(seconds=120)
    run = _run(seeded, queued_at=old)
    _event(seeded, run)

    reconciled = reconcile_orphaned_runs(seeded, older_than_seconds=60)

    assert reconciled == 0
    assert seeded.query(OutboxEvent).count() == 1


def test_reconcile_orphaned_runs_ignores_recently_queued_runs(seeded):
    _run(seeded)  # queued_at defaults to now

    reconciled = reconcile_orphaned_runs(seeded, older_than_seconds=60)

    assert reconciled == 0
    assert seeded.query(OutboxEvent).count() == 0
