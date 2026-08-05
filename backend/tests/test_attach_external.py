"""attach_result for work done outside AGMX (CTV2-1403).

Recording finished work used to require the task be 'dispatched', and the only
way there was to fire an agent to redo it. The record is the point of a task;
making the record cost a duplicate run made the system fight its own purpose.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Project, Task, TaskEvent
from app.services.task_orchestration import TaskOrchestrationService
from app.services.task_validators import PrerequisiteError


@pytest.fixture
def db():
    engine = create_engine(
        'sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Project(id="p1", name="P"))
    session.commit()
    yield session
    session.close()


def _task(db, task_id, status="todo"):
    db.add(Task(id=task_id, project="p1", title="t", status=status))
    db.commit()
    return db.get(Task, task_id)


def test_todo_task_can_attach_external_work(db):
    _task(db, "EXT-1", status="todo")
    TaskOrchestrationService(db).attach_result(
        task_id="EXT-1", commit="abc1234", external_executor="@coordinator",
    )
    task = db.get(Task, "EXT-1")
    assert task.status == "awaiting-review"
    assert task.result_ref == "abc1234"
    # Recorded as the executor, so the four-eyes rule still bites downstream.
    assert task.executor == "@coordinator"


def test_provenance_is_recorded_honestly(db):
    _task(db, "EXT-2", status="todo")
    TaskOrchestrationService(db).attach_result(
        task_id="EXT-2", commit="def5678", external_executor="@sub-agent-1",
    )
    event = (
        db.query(TaskEvent)
        .filter_by(task_id="EXT-2", event_type="attach_result")
        .one()
    )
    assert event.payload["provenance"] == "external"
    assert event.payload["external_executor"] == "@sub-agent-1"


def test_normal_attach_still_says_agent_run(db):
    _task(db, "NORM-1", status="dispatched")
    TaskOrchestrationService(db).attach_result(task_id="NORM-1", commit="aaa1111")
    event = (
        db.query(TaskEvent)
        .filter_by(task_id="NORM-1", event_type="attach_result")
        .one()
    )
    assert event.payload["provenance"] == "agent_run"


def test_todo_without_external_executor_is_still_rejected(db):
    """The escape hatch must stay explicit -- no silent relaxation."""
    _task(db, "STRICT-1", status="todo")
    with pytest.raises((PrerequisiteError, Exception)):
        TaskOrchestrationService(db).attach_result(task_id="STRICT-1", commit="bbb2222")
    assert db.get(Task, "STRICT-1").status == "todo"


def test_external_attach_never_marks_done(db):
    _task(db, "EXT-3", status="todo")
    with pytest.raises(PrerequisiteError):
        TaskOrchestrationService(db).attach_result(
            task_id="EXT-3", commit="ccc3333",
            option="done", external_executor="@coordinator",
        )
