"""Tests for the deadman monitor (CTV2-1400, redesigned in CTV2-1401).

Deadman answers ONE question about the whole system -- "is it still alive?" --
so it emits at most one event per stall, never one per task. The original
per-task sweep sent 232 Telegram messages on its first run; the regression
tests below pin the shape that made that impossible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AgentRun, Project, Setting, Task, TaskEvent
from app.services.deadman_service import (
    fire_deadman_events,
    get_no_progress_minutes,
    in_flight_run_count,
    system_stalled,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        'sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _make_task(db_session, task_id, *, status="dispatched", minutes_ago=45):
    proj_id = f"proj-{task_id}"
    if db_session.get(Project, proj_id) is None:
        db_session.add(Project(id=proj_id, name="P"))
    extra = {}
    if status == "done":
        extra = {"executor": "@a", "reviewer": "@b", "result_ref": "commit-1"}
    task = Task(id=task_id, project=proj_id, title="t", status=status, **extra)
    db_session.add(task)
    db_session.commit()
    task.updated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db_session.commit()
    return task


def _make_run(db_session, run_id, task, *, status="running", started_minutes_ago=45):
    run = AgentRun(
        id=run_id,
        task_id=task.id,
        agent_id="@a",
        cli="claude",
        command="echo hi",
        kind="execute",
        status=status,
        started_at=datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago),
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_default_no_progress_minutes_is_30(db_session):
    assert get_no_progress_minutes(db_session) == 30


def test_setting_overrides_default_minutes(db_session):
    db_session.add(Setting(key="deadman_no_progress_minutes", value="5"))
    db_session.commit()
    assert get_no_progress_minutes(db_session) == 5


def test_idle_system_is_never_stalled(db_session):
    """No accepted work outstanding -> quiet is correct, not a symptom.

    This is the regression that matters: 116 tasks parked in `todo` used to
    each fire an event. A backlog nobody promised to move says nothing about
    whether the machine is breathing.
    """
    for i in range(5):
        _make_task(db_session, f"BACKLOG-{i}", status="todo", minutes_ago=10_000)

    stalled, in_flight, _ = system_stalled(db_session, threshold_minutes=30)
    assert in_flight == 0
    assert stalled is False
    assert fire_deadman_events(db_session, threshold_minutes=30) == []


def test_in_flight_but_recently_progressed_is_not_stalled(db_session):
    task = _make_task(db_session, "BUSY-1")
    _make_run(db_session, "run-busy", task, started_minutes_ago=1)
    stalled, in_flight, _ = system_stalled(db_session, threshold_minutes=30)
    assert in_flight == 1
    assert stalled is False


def test_in_flight_and_quiet_fires_exactly_one_event(db_session):
    """Many stalled tasks, one signal -- the 232-message bug, pinned."""
    for i in range(10):
        task = _make_task(db_session, f"STUCK-{i}")
        _make_run(db_session, f"run-{i}", task, started_minutes_ago=45)

    events = fire_deadman_events(db_session, threshold_minutes=30)
    assert len(events) == 1
    assert events[0].event_type == "deadman"
    # System-wide, so it belongs to no single task.
    assert events[0].task_id is None
    assert events[0].payload["in_flight_runs"] == 10

    assert db_session.query(TaskEvent).filter_by(event_type="deadman").count() == 1


def test_deadman_does_not_repeat_without_progress(db_session):
    task = _make_task(db_session, "STALL-2")
    _make_run(db_session, "run-2", task, started_minutes_ago=45)

    assert len(fire_deadman_events(db_session, threshold_minutes=30)) == 1
    # A second poll tick with nothing moved in between.
    assert fire_deadman_events(db_session, threshold_minutes=30) == []
    assert db_session.query(TaskEvent).filter_by(event_type="deadman").count() == 1


def test_deadman_fires_again_after_progress_then_stall_again(db_session):
    """A repeat is only earned by the system actually moving in between."""
    t0 = datetime.now(timezone.utc)
    task = _make_task(db_session, "STALL-3")
    run = _make_run(db_session, "run-3", task, started_minutes_ago=45)

    assert len(fire_deadman_events(db_session, threshold_minutes=30, now=t0)) == 1
    assert fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=5)
    ) == []

    # Progress: a new run starts after the warning fired.
    run.started_at = t0 + timedelta(minutes=1)
    db_session.commit()

    # Progress alone is not a new stall yet.
    assert fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=2)
    ) == []

    # Quiet again since that progress -> a fresh stall may be reported.
    assert len(fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=35)
    )) == 1
    assert db_session.query(TaskEvent).filter_by(event_type="deadman").count() == 2


def test_finished_runs_do_not_count_as_in_flight(db_session):
    task = _make_task(db_session, "FIN-1")
    _make_run(db_session, "run-fin", task, status="success", started_minutes_ago=45)
    assert in_flight_run_count(db_session) == 0
    assert fire_deadman_events(db_session, threshold_minutes=30) == []


def test_queued_runs_count_as_in_flight(db_session):
    task = _make_task(db_session, "Q-1")
    _make_run(db_session, "run-q", task, status="queued", started_minutes_ago=45)
    assert in_flight_run_count(db_session) == 1


def test_deadman_payload_says_why_and_next(db_session):
    task = _make_task(db_session, "STALL-4")
    _make_run(db_session, "run-4", task, started_minutes_ago=45)
    payload = fire_deadman_events(db_session, threshold_minutes=30)[0].payload
    assert payload["no_progress_minutes"] >= 30
    assert payload["reason"]
    assert payload["next"]
