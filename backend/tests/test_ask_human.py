"""Tests for the ask_human tool (CTV2-1400).

Constraints from spec e6ee1eb0 / 017d9cd4: ask_human is one-way (no
get_answer/wait_for_human), why_human is mandatory, and a task asked about
must be labeled as waiting on a HUMAN, not a machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Project, Task, TaskEvent
from app.services.command_router import CommandRouter


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


@pytest.mark.asyncio
async def test_ask_human_rejects_empty_why_human(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {"question": "Should I delete prod data?", "why_human": ""},
        "session-1",
    )
    assert "error" in result
    assert "why_human" in result["error"]
    # No event should have been recorded for a rejected call.
    assert db_session.query(TaskEvent).filter_by(event_type="human_question").count() == 0


@pytest.mark.asyncio
async def test_ask_human_rejects_missing_why_human(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human", {"question": "Should I delete prod data?"}, "session-1",
    )
    assert "error" in result
    assert "why_human" in result["error"]


@pytest.mark.asyncio
async def test_ask_human_rejects_empty_question(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human", {"question": "", "why_human": "irreversible"}, "session-1",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_ask_human_is_one_way_and_returns_immediately(db_session):
    """No get_answer/wait_for_human exists; the call just queues and returns."""
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {"question": "Deploy to prod now?", "why_human": "spending real money"},
        "session-1",
    )
    assert result.get("action") == "asked"
    assert "note" in result
    assert "do not poll" in result["note"] or "chat" in result["note"]

    event = db_session.query(TaskEvent).filter_by(event_type="human_question").first()
    assert event is not None
    assert event.kind == "decision"
    assert event.payload["question"] == "Deploy to prod now?"
    assert event.payload["why_human"] == "spending real money"


@pytest.mark.asyncio
async def test_ask_human_without_task_id_does_not_require_one(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {"question": "General question", "why_human": "policy decision"},
        "session-1",
    )
    assert result.get("task_id") is None
    event = db_session.query(TaskEvent).filter_by(event_type="human_question").first()
    assert event.task_id is None


@pytest.mark.asyncio
async def test_ask_human_labels_task_as_waiting_on_human(db_session):
    """workflow_state must read waiting_human, not look like a machine hang."""
    db_session.add(Project(id="proj-ask", name="P"))
    db_session.add(Task(id="ASK-1", project="proj-ask", title="t", status="dispatched"))
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {
            "question": "Which migration strategy?",
            "why_human": "irreversible schema choice",
            "task_id": "ASK-1",
            "options": ["A", "B"],
        },
        "session-1",
    )
    assert result.get("action") == "asked"
    assert result.get("task_id") == "ASK-1"

    task = db_session.get(Task, "ASK-1")
    assert task.awaiting_approval is True
    assert task.workflow_state == "waiting_human"
    assert "Which migration strategy?" in (task.approval_prompt or "")

    event = db_session.query(TaskEvent).filter_by(event_type="human_question").first()
    assert event.task_id == "ASK-1"
    assert event.payload["options"] == ["A", "B"]


@pytest.mark.asyncio
async def test_ask_human_unknown_task_id_is_rejected(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {"question": "q", "why_human": "reason", "task_id": "NOPE-1"},
        "session-1",
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_ask_human_does_not_label_a_done_task(db_session):
    """A terminal task's awaiting_approval is DB-constrained to False; skip it."""
    db_session.add(Project(id="proj-done", name="P"))
    task = Task(
        id="DONE-1", project="proj-done", title="t", status="done",
        executor="@a", reviewer="@b", result_ref="commit-1",
    )
    db_session.add(task)
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "ask_human",
        {"question": "q", "why_human": "reason", "task_id": "DONE-1"},
        "session-1",
    )
    assert result.get("action") == "asked"
    db_session.refresh(task)
    assert task.awaiting_approval is False


def test_ask_human_tool_has_no_get_answer_or_poll_sibling():
    """Spec 017d9cd4: get_answer/wait_for_human must never be registered."""
    from app.services.tool_registry import TOOL_REGISTRY

    assert "ask_human" in TOOL_REGISTRY
    assert "get_answer" not in TOOL_REGISTRY
    assert "wait_for_human" not in TOOL_REGISTRY
