"""`awaiting_approval` is derived from evidence, never asserted (CTV2-1401).

Measured on the live DB on 2026-08-06: 20 tasks carried the flag and four of
them (CTLA-005, CTV2-1372, VOMA-003, VOMA-015) had no unresolved gate at all.
Nothing was waiting on them and nothing could clear them -- `approve_gate`
answers "No pending gate found" and every dispatch path refused the task.
Permanently bricked by a projection that had drifted off the truth.

These tests pin the two halves of the repair: the derivation sees every real
source of waiting (including the one that is not a gate), and a drifted column
can no longer block anything.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import GateRecord, Project, Task, TaskEvent
from app.services.approval_hold import derive_approval_hold, task_is_waiting_on_human
from app.services.task_state_machine import TaskStateMachine


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Project(id="p1", name="P"))
    session.add(Task(id="T-1", project="p1", title="t", status="todo"))
    session.commit()
    yield session
    session.close()


def _task(db) -> Task:
    return db.get(Task, "T-1")


def _gate(db, *, status="pending", parent_id=None, key="g1", prompt=None) -> GateRecord:
    record = GateRecord(
        task_id="T-1",
        gate_type="dispatch",
        status=status,
        actor="chat:test",
        idempotency_key=key,
        input_hash="h" * 64,
        input_payload={"approval_prompt": prompt} if prompt else {},
        parent_id=parent_id,
    )
    db.add(record)
    db.commit()
    return record


def test_no_evidence_means_no_hold(db):
    assert derive_approval_hold(db, _task(db)) is None


def test_pending_gate_root_holds_and_carries_its_own_prompt(db):
    _gate(db, prompt="duyệt executor?")
    hold = derive_approval_hold(db, _task(db))
    assert hold is not None
    assert hold.source == "gate"
    assert hold.prompt == "duyệt executor?"


def test_a_decided_gate_root_does_not_hold(db):
    """The drift shape itself.

    GateRecord is append-only, so an approved gate leaves its root row saying
    `pending` forever and the decision arrives as a child.  Reading the root's
    status alone is what let 14 tasks sit locked with nothing waiting on them.
    """
    root = _gate(db)
    _gate(db, status="approved", parent_id=root.id, key="g1:decision")
    assert derive_approval_hold(db, _task(db)) is None


def test_a_drifted_column_cannot_block_anything(db):
    """The column may be wrong for one beat; it may not be believed."""
    task = _task(db)
    task.awaiting_approval = True
    task.approval_prompt = "chờ cái không tồn tại"
    db.commit()

    assert task_is_waiting_on_human(db, task) is False
    # ...and the next sync writes the truth back over it.
    TaskStateMachine(db).sync_awaiting_approval(task)
    assert task.awaiting_approval is False
    assert task.approval_prompt is None


def test_ask_human_holds_without_any_gate(db):
    """The one wait that is legitimately not in the gate ledger.

    `ask_human` is answered by typing in chat, through no tool at all, so a
    derivation that only looked at gates would silently unblock a task a human
    is still thinking about.
    """
    db.add(
        TaskEvent(
            task_id="T-1",
            event_type="human_question",
            kind="decision",
            payload={"question": "A hay B?"},
        )
    )
    db.commit()

    hold = derive_approval_hold(db, _task(db))
    assert hold is not None
    assert hold.source == "human_question"
    assert hold.prompt == "[human_question] A hay B?"


def test_an_answer_retires_the_question(db):
    db.add(
        TaskEvent(
            task_id="T-1", event_type="human_question", kind="decision",
            payload={"question": "A hay B?"},
        )
    )
    db.commit()
    db.add(TaskEvent(task_id="T-1", event_type="human_answer", kind="info", payload={}))
    db.commit()

    assert derive_approval_hold(db, _task(db)) is None


def test_a_question_asked_after_the_last_answer_holds_again(db):
    for event_type in ("human_question", "human_answer"):
        db.add(TaskEvent(task_id="T-1", event_type=event_type, kind="info", payload={}))
        db.commit()
    db.add(
        TaskEvent(
            task_id="T-1", event_type="human_question", kind="decision",
            payload={"question": "còn cái này nữa?"},
        )
    )
    db.commit()

    hold = derive_approval_hold(db, _task(db))
    assert hold is not None and hold.source == "human_question"


def test_open_questions_hold_through_the_spec_clarity_loop(db):
    task = _task(db)
    task.spec_clarity = "medium"
    task.open_questions = ["repo nào?"]
    db.commit()

    hold = derive_approval_hold(db, task)
    assert hold is not None and hold.source == "spec_clarity"
    assert "repo nào?" in hold.prompt

    task.spec_clarity = "high"
    task.open_questions = []
    db.commit()
    assert derive_approval_hold(db, task) is None


def test_missing_spec_clarity_is_not_a_low_spec_clarity(db):
    """Legacy tasks predate the column; absence of a measurement is not a bad one."""
    task = _task(db)
    task.spec_clarity = None
    task.open_questions = []
    db.commit()
    assert derive_approval_hold(db, task) is None


def test_a_critic_rejection_holds_until_the_plan_is_regenerated(db):
    task = _task(db)
    task.plan_critic = "@critic"
    task.plan_critic_status = "reject"
    task.plan_critic_findings = [{"title": "thiếu bằng chứng", "evidence": "x"}]
    db.commit()

    hold = derive_approval_hold(db, task)
    assert hold is not None and hold.source == "plan_critic"
    assert "thiếu bằng chứng" in hold.prompt

    task.plan_critic_status = None
    db.commit()
    assert derive_approval_hold(db, task) is None


def test_a_failed_landing_holds_until_the_repo_is_fixed(db):
    task = _task(db)
    task.status = "awaiting-review"
    task.result_ref = "base..head"
    task.error = "landing_failed: merge conflict in app/x.py"
    db.commit()

    hold = derive_approval_hold(db, task)
    assert hold is not None and hold.source == "landing"
    assert "merge conflict" in hold.prompt

    task.error = None
    db.commit()
    assert derive_approval_hold(db, task) is None


def test_terminal_tasks_never_hold(db):
    """`ck_tasks_terminal_not_awaiting_approval` forbids it, and nobody is waiting.

    `cancelled` is written for real; `done` is checked through `as_status`
    because reaching it for real needs the whole verdict invariant
    (`ck_tasks_done_invariants`), which is a different claim than this one.
    """
    _gate(db, prompt="ai đó duyệt đi")
    task = _task(db)
    assert derive_approval_hold(db, task, as_status="done") is None
    task.status = "cancelled"
    db.commit()
    assert derive_approval_hold(db, task) is None


def test_as_status_answers_for_the_status_about_to_be_written(db):
    """`transition_to_done` writes the projection before flipping the status."""
    _gate(db, prompt="ai đó duyệt đi")
    task = _task(db)
    assert derive_approval_hold(db, task) is not None
    assert derive_approval_hold(db, task, as_status="done") is None


def test_a_gate_outranks_the_other_holds(db):
    """A gate is the only hold with an id and a tool pointed at it -- name it first."""
    task = _task(db)
    task.spec_clarity = "low"
    task.open_questions = ["?"]
    db.commit()
    _gate(db, prompt="duyệt executor?")

    hold = derive_approval_hold(db, task)
    assert hold is not None and hold.source == "gate"
    assert hold.gate_record_id is not None


def test_sync_writes_both_columns_from_the_same_hold(db):
    """The flag and the prompt can never disagree: one source, one write."""
    task = _task(db)
    _gate(db, prompt="duyệt executor?")
    TaskStateMachine(db).sync_awaiting_approval(task)
    assert task.awaiting_approval is True
    assert task.approval_prompt == "duyệt executor?"
