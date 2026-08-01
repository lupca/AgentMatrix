"""CTV2-234 (re-dispatch after changes-requested), CTV2-228 (agent_id alias),
and CTV2-235 (no-commit task completion)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.models import Agent, GateRecord, Project, Task
from app.services.command_router import CommandRouter
from app.services.task_orchestration import (
    PrerequisiteError,
    TaskOrchestrationService,
)


def _seed(db, task_status="todo", tags=None):
    db.add(Project(id="p1", name="P", repo_root="/tmp"))
    db.add(Agent(id="@exec-a", name="A", role="executor", cli="codex"))
    db.add(Agent(id="@exec-b", name="B", role="executor", cli="codex"))
    db.add(Task(
        id="T-1", project="p1", title="Task", status=task_status,
        mode="supervised", acceptance_criteria=["ok"], tags=tags or [],
        executor="@exec-a" if task_status != "todo" else None,
    ))
    db.commit()


def _dispatch(db, agent_id="@exec-b"):
    with patch(
        "app.services.task_orchestration.build_dispatch_command",
        return_value=("codex exec task", "/tmp", "codex"),
    ):
        return TaskOrchestrationService(db).request_dispatch(
            task_id="T-1", agent_id=agent_id, actor="@op",
            idempotency_key=f"d-{agent_id}",
        )


def test_dispatch_accepted_from_changes_requested(db_session):
    _seed(db_session, task_status="changes-requested")
    result = _dispatch(db_session)
    assert result.gate_record.gate_type == "dispatch"
    assert result.gate_record.status == "pending"


def test_dispatch_accepted_from_failed(db_session):
    _seed(db_session, task_status="failed")
    result = _dispatch(db_session)
    assert result.gate_record.status == "pending"


@pytest.mark.asyncio
async def test_dispatch_tool_accepts_agent_id_alias(db_session):
    """CTV2-228: 'agent_id' used to be silently dropped -> matcher took over."""
    _seed(db_session)
    router = CommandRouter(db_session)
    with patch(
        "app.services.task_orchestration.build_dispatch_command",
        return_value=("codex exec task", "/tmp", "codex"),
    ):
        result = await router.execute_tool(
            "dispatch_task", {"task_id": "T-1", "agent_id": "@exec-b"}, "s1"
        )
    assert result.get("action") == "dispatch_pending", result
    gate = db_session.query(GateRecord).filter_by(
        task_id="T-1", gate_type="dispatch", status="pending"
    ).first()
    assert gate.input_payload["agent_id"] == "@exec-b"


def test_no_commit_completion_requires_the_tag(db_session):
    _seed(db_session, task_status="dispatched")
    with pytest.raises(PrerequisiteError, match="no-commit"):
        TaskOrchestrationService(db_session).complete_no_commit_task(
            task_id="T-1", actor="agent:@exec-a", run_id="r1"
        )


def test_no_commit_completion_marks_done_with_system_verdict(db_session):
    _seed(db_session, task_status="dispatched", tags=["no-commit"])
    result = TaskOrchestrationService(db_session).complete_no_commit_task(
        task_id="T-1", actor="agent:@exec-a", run_id="r1"
    )
    assert result["action"] == "no_commit_completed"

    task = db_session.get(Task, "T-1")
    assert task.status == "done"
    assert task.result_ref == "no-commit"
    assert task.final_verdict == "pass"
    assert task.reviewer == "@system-no-commit"
    gate = db_session.query(GateRecord).filter_by(
        task_id="T-1", gate_type="verdict", status="approved"
    ).first()
    assert gate is not None and gate.output_ref == "pass"
    assert gate.input_payload["kind"] == "no_commit_completion"
