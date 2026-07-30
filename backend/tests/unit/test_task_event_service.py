"""Unit tests for TaskEventService and emit function (CTV2-114)."""

import uuid
from datetime import datetime, timezone

import pytest
from app.db.models import (
    Project,
    Session as ChatSession,
    SessionEventCursor,
    Task,
    TaskEvent,
)
from app.services.task_event_service import (
    DECISION_EVENT_TYPES,
    TaskEventService,
    emit_task_event,
)


@pytest.fixture
def sample_task(db_session):
    """Create a unique sample project and task in the test DB."""
    uid = str(uuid.uuid4())[:8]
    proj_id = f"proj-{uid}"
    task_id = f"T-{uid}"
    project = Project(id=proj_id, name=f"Project {uid}")
    task = Task(
        id=task_id,
        project=proj_id,
        title=f"Test Task {uid}",
        status="todo",
    )
    db_session.add(project)
    db_session.add(task)
    db_session.commit()
    return task


def test_emit_task_event_basic(db_session, sample_task):
    """AC3 & AC4: TaskEventService.emit records event in DB with correct fields."""
    payload = {"run_id": "run-123", "agent": "claude-opus"}
    event = TaskEventService.emit(
        task_id=sample_task.id,
        event_type="dispatched",
        payload=payload,
        db=db_session,
    )

    assert event is not None
    assert event.id is not None
    assert event.task_id == sample_task.id
    assert event.event_type == "dispatched"
    assert event.payload == payload
    assert event.created_at is not None
    assert event.consumed_at is None

    # Verify directly from DB query
    saved_event = db_session.query(TaskEvent).filter_by(id=event.id).first()
    assert saved_event is not None
    assert saved_event.event_type == "dispatched"
    assert saved_event.payload == payload


def test_emit_task_event_default_payload(db_session, sample_task):
    """Emit with None payload defaults to empty dict."""
    event = emit_task_event(
        task_id=sample_task.id,
        event_type="running",
        payload=None,
        db=db_session,
    )

    assert event.payload == {}


def test_emit_service_instance(db_session, sample_task):
    """TaskEventService instance emit method works correctly."""
    service = TaskEventService(db=db_session)
    event = service.emit(
        task_id=sample_task.id,
        event_type="gate_pending",
        payload={"gate": "spec", "gate_record_id": 42},
    )

    assert event.id is not None
    assert event.event_type == "gate_pending"
    assert event.payload["gate"] == "spec"


def test_get_task_events_filtering(db_session, sample_task):
    """Test get_task_events filtering by task_id, since, and event_types."""
    service = TaskEventService(db=db_session)

    e1 = service.emit(sample_task.id, "dispatched", {"step": 1})
    e2 = service.emit(sample_task.id, "running", {"step": 2})
    e3 = service.emit(sample_task.id, "done", {"step": 3})

    # Filter by task_id
    events = service.get_events(task_id=sample_task.id)
    assert len(events) == 3
    assert [e.event_type for e in events] == ["dispatched", "running", "done"]

    # Filter by event_types
    filtered = service.get_events(task_id=sample_task.id, event_types=["dispatched", "done"])
    assert len(filtered) == 2
    assert [e.event_type for e in filtered] == ["dispatched", "done"]

    # Filter by since
    now = datetime.now(timezone.utc)
    future_events = service.get_events(task_id=sample_task.id, since=now)
    assert len(future_events) == 0


def test_mark_consumed(db_session, sample_task):
    """Test mark_task_events_consumed updates consumed_at timestamp."""
    service = TaskEventService(db=db_session)
    e1 = service.emit(sample_task.id, "gate_passed", {"gate": "plan"})
    e2 = service.emit(sample_task.id, "done", {"result_ref": "hash123"})

    count = service.mark_consumed([e1.id, e2.id])
    assert count == 2

    db_session.refresh(e1)
    db_session.refresh(e2)
    assert e1.consumed_at is not None
    assert e2.consumed_at is not None


def test_cascade_delete_task(db_session, sample_task):
    """Deleting a Task cascades and deletes associated TaskEvents."""
    service = TaskEventService(db=db_session)
    event = service.emit(sample_task.id, "running", {"pid": 9999})
    event_id = event.id

    # Delete task
    db_session.delete(sample_task)
    db_session.commit()

    # Verify event is deleted via CASCADE
    deleted_event = db_session.query(TaskEvent).filter_by(id=event_id).first()
    assert deleted_event is None


def test_claim_event_is_atomic_and_keeps_winning_session(db_session, sample_task):
    sessions = [ChatSession() for _ in range(2)]
    db_session.add_all(sessions)
    db_session.commit()
    event = emit_task_event(sample_task.id, "run_failed", {"error": "boom"}, db=db_session)
    service = TaskEventService(db_session)

    assert service.claim_event(event.id, sessions[0].id) is True
    assert service.claim_event(event.id, sessions[1].id) is False

    db_session.refresh(event)
    assert event.claimed_by_session_id == sessions[0].id


def test_digest_uses_independent_session_cursors(db_session, sample_task):
    sessions = [ChatSession() for _ in range(2)]
    db_session.add_all(sessions)
    db_session.commit()
    events = [
        emit_task_event(sample_task.id, "running", {"index": i}, db=db_session)
        for i in range(5)
    ]
    db_session.add_all(
        [
            SessionEventCursor(
                session_id=sessions[0].id,
                last_digest_event_id=0,
            ),
            SessionEventCursor(
                session_id=sessions[1].id,
                last_digest_event_id=events[2].id,
            ),
        ]
    )
    db_session.commit()
    service = TaskEventService(db_session)

    assert [event.id for event in service.get_digest(sessions[0].id, limit=10)] == [
        event.id for event in events
    ]
    assert [event.id for event in service.get_digest(sessions[1].id, limit=10)] == [
        events[3].id,
        events[4].id,
    ]

    for session in sessions:
        service.advance_cursor(session.id, events[-1].id)
        assert service.get_digest(session.id, limit=10) == []


@pytest.mark.parametrize("event_type", sorted(DECISION_EVENT_TYPES))
def test_emit_infers_decision_kind_from_event_type(
    db_session, sample_task, event_type
):
    decision = emit_task_event(sample_task.id, event_type, {}, db=db_session)
    assert decision.kind == "decision"


def test_emit_infers_info_kind_for_other_event_types(db_session, sample_task):
    info = emit_task_event(sample_task.id, "done", {}, db=db_session)
    assert info.kind == "info"
