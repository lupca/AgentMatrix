"""CTV2-1396: open_questions/spec_clarity must say when they're stale.

task.open_questions and task.spec_clarity are only overwritten when a plan
run finishes (write_spec_plan). While a new plan run is queued/running,
these columns still hold the PREVIOUS round's values. On 2026-08-05
(UIKI-006) the coordinator read those stale questions, concluded the
planner had ignored its answer, and answered again -- burning an extra real
planner round for nothing. The fix: get_status (and the dispatch
prerequisite error) must say, in one read, "these are from a previous
round, a run is already in flight, don't answer again."
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Agent, AgentRun, Project, Task
from app.services.command_router import CommandRouter
from app.services.task_orchestration import TaskOrchestrationService
from app.services.task_state_machine import find_active_plan_run


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Stale open questions task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
        "spec_clarity": "medium",
        "open_questions": ["What auth mechanism should this use?"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def _add_active_plan_run(db, task_id: str, run_id: str = "run-active") -> AgentRun:
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        agent_id="@executor",
        cli="codex",
        command="claude",
        kind="execute",
        status="running",
        idempotency_key=f"planner:{run_id}",
        queued_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.add(run)
    db.commit()
    return run


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_flags_stale_open_questions_when_plan_run_active(
    service, db_session
):
    _add_task(db_session, "STALE-001")
    run = _add_active_plan_run(db_session, "STALE-001")

    router = CommandRouter(db_session)
    response = await router._handle_get_status("STALE-001", "")

    task_payload = response["task"]
    # values are preserved, not erased
    assert task_payload["open_questions"] == ["What auth mechanism should this use?"]
    assert task_payload["spec_clarity"] == "medium"

    status = task_payload["open_questions_status"]
    assert set(status) == {"state", "why", "next", "active_run_id"}
    assert status["active_run_id"] == run.id
    assert run.id in status["why"]
    assert "đừng trả lời lại" in status["next"].lower() or "wait" in status["next"].lower() or "chờ" in status["next"].lower()


@pytest.mark.asyncio
async def test_get_status_no_stale_marker_without_active_plan_run(service, db_session):
    _add_task(db_session, "STALE-002")

    router = CommandRouter(db_session)
    response = await router._handle_get_status("STALE-002", "")

    task_payload = response["task"]
    assert task_payload["open_questions"] == ["What auth mechanism should this use?"]
    assert "open_questions_status" not in task_payload


@pytest.mark.asyncio
async def test_get_status_no_stale_marker_when_no_open_questions_even_with_active_run(
    service, db_session
):
    _add_task(db_session, "STALE-003", spec_clarity=None, open_questions=[])
    _add_active_plan_run(db_session, "STALE-003")

    router = CommandRouter(db_session)
    response = await router._handle_get_status("STALE-003", "")

    task_payload = response["task"]
    assert "open_questions_status" not in task_payload


@pytest.mark.asyncio
async def test_get_status_ignores_finished_plan_runs(service, db_session):
    _add_task(db_session, "STALE-004")
    finished = _add_active_plan_run(db_session, "STALE-004", run_id="run-done")
    finished.status = "success"
    db_session.commit()

    router = CommandRouter(db_session)
    response = await router._handle_get_status("STALE-004", "")

    assert "open_questions_status" not in response["task"]


# ---------------------------------------------------------------------------
# find_active_plan_run helper (shared by get_status, update_task, dispatch)
# ---------------------------------------------------------------------------


def test_find_active_plan_run_matches_queued_or_running_planner_runs(db_session):
    _add_task(db_session, "STALE-005")
    run = _add_active_plan_run(db_session, "STALE-005")

    found = find_active_plan_run(db_session, "STALE-005")
    assert found is not None
    assert found.id == run.id


def test_find_active_plan_run_ignores_non_planner_runs(db_session):
    _add_task(db_session, "STALE-006")
    run = AgentRun(
        id="run-exec",
        task_id="STALE-006",
        agent_id="@executor",
        cli="codex",
        command="claude",
        kind="execute",
        status="running",
        idempotency_key="exec:something",
        queued_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()

    assert find_active_plan_run(db_session, "STALE-006") is None


# ---------------------------------------------------------------------------
# dispatch prerequisite error also names the in-flight run
# ---------------------------------------------------------------------------


def test_dispatch_open_questions_error_names_active_plan_run(service, db_session):
    from app.services.task_orchestration import OrchestrationError

    _add_task(db_session, "STALE-007")
    run = _add_active_plan_run(db_session, "STALE-007")

    with pytest.raises(OrchestrationError) as excinfo:
        service.request_dispatch(
            task_id="STALE-007",
            agent_id="@executor",
            actor="chat:test",
            idempotency_key="dispatch-1",
        )

    message = str(excinfo.value)
    assert run.id in message
    assert "previous" in message.lower() or "PREVIOUS" in message


def test_dispatch_open_questions_error_plain_without_active_run(service, db_session):
    from app.services.task_orchestration import OrchestrationError

    _add_task(db_session, "STALE-008")

    with pytest.raises(OrchestrationError) as excinfo:
        service.request_dispatch(
            task_id="STALE-008",
            agent_id="@executor",
            actor="chat:test",
            idempotency_key="dispatch-1",
        )

    message = str(excinfo.value)
    assert "unanswered open questions" in message
    assert "PREVIOUS" not in message
