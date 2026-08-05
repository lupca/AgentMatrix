"""ask_human must be able to un-block what it blocked (CTV2-1405).

Asking sets `awaiting_approval`, which every transition tool refuses on. The
answer arrives in chat, through no tool at all -- so without an answer mode
nothing could ever lower the flag and the task was bricked. The tool that
raises a block has to be able to lower it.
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
def db():
    engine = create_engine(
        'sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Project(id="p1", name="P"))
    s.add(Task(id="Q-1", project="p1", title="t", status="todo"))
    s.commit()
    yield s
    s.close()


async def _ask(db, **kw):
    return await CommandRouter(db).execute_tool("ask_human", kw, "sess-1")


@pytest.mark.asyncio
async def test_asking_blocks_and_answering_unblocks(db):
    await _ask(db, task_id="Q-1", question="A hay B?", why_human="tiêu tiền thật")
    assert db.get(Task, "Q-1").awaiting_approval is True

    res = await _ask(db, task_id="Q-1", answer="Chọn B")
    assert res["action"] == "answered"
    assert res["unblocked"] is True
    assert db.get(Task, "Q-1").awaiting_approval is False
    assert db.get(Task, "Q-1").approval_prompt is None


@pytest.mark.asyncio
async def test_answer_is_stored_verbatim(db):
    await _ask(db, task_id="Q-1", question="A hay B?", why_human="tiêu tiền thật")
    await _ask(db, task_id="Q-1", answer="Chọn B, đừng hỏi lại")
    event = db.query(TaskEvent).filter_by(task_id="Q-1", event_type="human_answer").one()
    assert event.payload["answer"] == "Chọn B, đừng hỏi lại"


@pytest.mark.asyncio
async def test_answer_does_not_clear_a_real_gate(db):
    """A genuine gate still needs approve_gate -- no back door."""
    task = db.get(Task, "Q-1")
    task.awaiting_approval = True
    task.approval_prompt = "[dispatch] duyệt executor?"
    db.commit()

    res = await _ask(db, task_id="Q-1", answer="ừ")
    assert res["unblocked"] is False
    assert db.get(Task, "Q-1").awaiting_approval is True
    assert "approve_gate" in res["note"]


@pytest.mark.asyncio
async def test_answer_requires_task_id(db):
    res = await _ask(db, answer="ừ")
    assert "error" in res


@pytest.mark.asyncio
async def test_asking_still_requires_why_human(db):
    res = await _ask(db, task_id="Q-1", question="A hay B?")
    assert "error" in res
    assert "why_human" in res["error"]
