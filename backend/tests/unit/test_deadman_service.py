"""Tests for the deadman monitor (CTV2-1400).

Fires exactly one `deadman` TaskEvent per stall; only fires again once
progress (a `Task.updated_at` bump) has happened since the last one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Project, Setting, Task, TaskEvent
from app.services.deadman_service import (
    fire_deadman_events,
    find_stalled_tasks,
    get_no_progress_minutes,
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


def test_default_no_progress_minutes_is_30(db_session):
    assert get_no_progress_minutes(db_session) == 30


def test_setting_overrides_default_minutes(db_session):
    db_session.add(Setting(key="deadman_no_progress_minutes", value="5"))
    db_session.commit()
    assert get_no_progress_minutes(db_session) == 5


def test_fresh_task_is_not_stalled(db_session):
    _make_task(db_session, "FRESH-1", minutes_ago=1)
    stalled = find_stalled_tasks(db_session, threshold_minutes=30)
    assert stalled == []


def test_stalled_task_fires_exactly_one_deadman_event(db_session):
    _make_task(db_session, "STALL-1", minutes_ago=45)
    events = fire_deadman_events(db_session, threshold_minutes=30)
    assert len(events) == 1
    assert events[0].event_type == "deadman"
    assert events[0].task_id == "STALL-1"

    all_deadman = db_session.query(TaskEvent).filter_by(event_type="deadman").all()
    assert len(all_deadman) == 1


def test_deadman_does_not_repeat_without_progress(db_session):
    """Same stall, polled twice: only one event total (the bug this fixes)."""
    _make_task(db_session, "STALL-2", minutes_ago=45)

    first = fire_deadman_events(db_session, threshold_minutes=30)
    assert len(first) == 1

    # A second poll tick, nothing changed on the task in between.
    second = fire_deadman_events(db_session, threshold_minutes=30)
    assert second == []

    all_deadman = db_session.query(TaskEvent).filter_by(event_type="deadman", task_id="STALL-2").all()
    assert len(all_deadman) == 1


def test_deadman_fires_again_after_progress_then_stall_again(db_session):
    """Repeat firing is only allowed once *progress* (Task.updated_at moving
    forward) has happened since the last deadman. `now` is passed explicitly
    so the test can simulate time passing without sleeping or fighting the
    DB's real-wall-clock `created_at` on the deadman TaskEvent."""
    t0 = datetime.now(timezone.utc)
    task = _make_task(db_session, "STALL-3", minutes_ago=45)

    first = fire_deadman_events(db_session, threshold_minutes=30, now=t0)
    assert len(first) == 1

    # Not stalled yet at t0 + 5min with no further progress -- no repeat.
    still_suppressed = fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=5)
    )
    assert still_suppressed == []

    # Progress: something touches the task (e.g. a new run) after the
    # deadman fired at t0.
    task.updated_at = t0 + timedelta(minutes=1)
    db_session.commit()

    # That progress alone isn't a new stall yet.
    still_fresh = fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=2)
    )
    assert still_fresh == []

    # Time passes with no further progress: stalled again relative to the
    # new `now`, and there WAS progress since the last deadman fired.
    second = fire_deadman_events(
        db_session, threshold_minutes=30, now=t0 + timedelta(minutes=35)
    )
    assert len(second) == 1

    all_deadman = db_session.query(TaskEvent).filter_by(event_type="deadman", task_id="STALL-3").all()
    assert len(all_deadman) == 2


def test_done_and_cancelled_tasks_are_never_stalled(db_session):
    _make_task(db_session, "DONE-1", status="done", minutes_ago=100)
    _make_task(db_session, "CANCELLED-1", status="cancelled", minutes_ago=100)
    stalled = find_stalled_tasks(db_session, threshold_minutes=30)
    ids = {t.id for t in stalled}
    assert "DONE-1" not in ids
    assert "CANCELLED-1" not in ids


def test_deadman_payload_carries_reason_and_minutes(db_session):
    _make_task(db_session, "STALL-4", minutes_ago=45)
    events = fire_deadman_events(db_session, threshold_minutes=30)
    payload = events[0].payload
    assert payload["no_progress_minutes"] >= 30
    assert "reason" in payload and payload["reason"]
