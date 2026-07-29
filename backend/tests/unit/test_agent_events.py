"""Tests for CTV2-209 event normalization and persistence."""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import AgentEvent, AgentRun, Project, Task, VendorRawEvent
from app.workers.agent_runner import parse_vendor_event


@pytest.mark.parametrize(
    ("cli", "line", "event_type"),
    [
        ("codex", {"type": "thread.started", "id": "thread-1"}, "run.started"),
        ("codex", {"type": "item.completed", "item": {"type": "command"}}, "tool.completed"),
        ("claude", {"type": "tool_use", "name": "Bash"}, "tool.started"),
        ("claude", {"type": "result", "result": "done"}, "run.completed"),
        ("agy", {"status": "response", "text": "hello"}, "llm.completed"),
        ("unknown", {"tool_name": "shell", "input": "pwd"}, "tool.started"),
    ],
)
def test_parse_vendor_event_normalizes_vendor_lines(cli, line, event_type):
    parsed = parse_vendor_event(cli, json.dumps(line))

    assert len(parsed) == 1
    assert parsed[0]["event_type"] == event_type
    assert parsed[0]["payload"] == line


def test_parse_vendor_event_retains_plain_text():
    parsed = parse_vendor_event("codex", "plain output")

    assert parsed[0]["event_type"] == "llm.completed"
    assert parsed[0]["payload"] == {"text": "plain output", "stream": "stdout"}


def test_parse_vendor_event_preserves_iso_timestamp():
    parsed = parse_vendor_event("codex", '{"type":"turn.started","timestamp":"2026-07-29T10:20:30Z"}')

    assert parsed[0]["timestamp"] == datetime(2026, 7, 29, 10, 20, 30, tzinfo=timezone.utc)


@pytest.fixture
def agent_run(db_session):
    project = Project(id="events-project", name="Events project")
    task = Task(id="EVENTS-001", project=project.id, title="Event persistence")
    run = AgentRun(
        id="events-run-001",
        task_id=task.id,
        agent_id="@agent",
        cli="codex",
        command="echo test",
    )
    db_session.add_all([project, task, run])
    db_session.commit()
    return run


def test_agent_and_vendor_event_models_persist_and_relate(db_session, agent_run):
    normalized = AgentEvent(
        run_id=agent_run.id,
        seq=0,
        event_type="run.started",
        payload={"thread_id": "thread-1"},
    )
    raw = VendorRawEvent(
        run_id=agent_run.id,
        seq=0,
        cli="codex",
        raw_output='{"type":"thread.started"}',
    )
    db_session.add_all([normalized, raw])
    db_session.commit()
    db_session.refresh(agent_run)

    assert agent_run.agent_events == [normalized]
    assert agent_run.vendor_raw_events == [raw]
    assert normalized.run is agent_run
    assert raw.run is agent_run


def test_event_models_reject_duplicate_or_negative_sequences(db_session, agent_run):
    db_session.add(AgentEvent(run_id=agent_run.id, seq=0, event_type="run.started", payload={}))
    db_session.commit()
    db_session.add(AgentEvent(run_id=agent_run.id, seq=0, event_type="run.completed", payload={}))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    db_session.add(VendorRawEvent(run_id=agent_run.id, seq=-1, cli="codex", raw_output="bad"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_event_models_cascade_when_run_is_deleted(db_session, agent_run):
    db_session.add_all([
        AgentEvent(run_id=agent_run.id, seq=0, event_type="run.started", payload={}),
        VendorRawEvent(run_id=agent_run.id, seq=0, cli="codex", raw_output="start"),
    ])
    db_session.commit()
    db_session.delete(agent_run)
    db_session.commit()

    assert db_session.query(AgentEvent).filter_by(run_id=agent_run.id).count() == 0
    assert db_session.query(VendorRawEvent).filter_by(run_id=agent_run.id).count() == 0
