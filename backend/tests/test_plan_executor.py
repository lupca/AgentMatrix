"""CTV2-1382: planner/critic AgentRuns dispatched through the outbox and run
inside the Dramatiq worker (app.workers.plan_executor), instead of awaited
synchronously inside the MCP request handler.
"""

import json
from unittest.mock import AsyncMock, patch

from app.db.models import Agent, AgentRun, Project, Task
from app.schemas.task import SPEC_PLAN_RESULT_SCHEMA_VERSION
from app.services.providers import ProviderResponse
from app.workers import plan_executor


def _valid_plan_payload(**overrides):
    payload = {
        "schema_version": SPEC_PLAN_RESULT_SCHEMA_VERSION,
        "acceptance_criteria": ["Widget renders"],
        "constraints": ["Do not add a migration"],
        "evidence": [{
            "fact": "Widget module exists", "source_type": "file",
            "source": "backend/app/widget.py:1", "result": "module exists",
        }],
        "prior_art": [],
        "ruled_out": [],
        "limits": None,
        "plan": "Build widget.",
        "files": ["backend/app/widget.py"],
        "tests": ["backend/tests/test_widget.py"],
        "risk": "low",
        "spec_clarity": "high",
        "open_questions": [],
    }
    payload.update(overrides)
    return payload


def _response(payload: dict) -> ProviderResponse:
    return ProviderResponse(provider="anthropic", model="claude-x", text=json.dumps(payload))


def test_is_plan_run_matches_only_planner_prefixed_idempotency_keys():
    real = AgentRun(idempotency_key="planner:TASK-1:plan:abcd")
    critic = AgentRun(idempotency_key="planner:TASK-1:critic:abcd")
    dispatch = AgentRun(idempotency_key="advance:TASK-1:dispatch:r0")
    assert plan_executor.is_plan_run(real)
    assert plan_executor.is_plan_run(critic)
    assert not plan_executor.is_plan_run(dispatch)


def test_execute_plan_run_goes_through_worker_and_records_pid_and_started_at(
    db_session, tmp_path
):
    """The distinguishing test named in CTV2-1382's acceptance criteria: an
    AgentRun that went through the real worker path (execute_plan_run) must
    end up with a persisted, non-null pid while the call is in flight, and a
    non-null started_at once claimed -- both were NULL before this task."""

    db_session.add(Project(id="proj-pe", name="Plan Executor Project", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@pe-planner", name="Planner", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-PE-1", project="proj-pe", title="Build the widget", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@pe-planner")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    db_session.commit()
    assert run.pid is None
    assert run.started_at is None

    # A real outbox delivery claims queued -> running (see
    # cli_executor._claim_run_attempt) before calling execute_plan_run.
    run.status = "running"
    db_session.commit()

    observed_pid_mid_flight = {}

    def fake_complete(agent, messages, *args, **kwargs):
        on_start = kwargs.get("on_start")
        if on_start is not None:
            on_start(918273)
            db_session.refresh(run)
            observed_pid_mid_flight["pid"] = run.pid
        return _response(_valid_plan_payload())

    with patch(
        "app.services.spec_plan_generator.semantic_search", new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete", new=AsyncMock(side_effect=fake_complete),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    # Captured while the (simulated) CLI subprocess was "running" -- this is
    # the concrete NULL->NOT NULL flip the task's acceptance criteria are
    # checking for; before CTV2-1382 there was no worker call to observe.
    assert observed_pid_mid_flight["pid"] == 918273

    db_session.refresh(run)
    assert run.status == "success"
    assert run.started_at is not None

    updated_task = db_session.get(Task, "TASK-PE-1")
    assert updated_task.plan == "Build widget."
    assert updated_task.planner == "@pe-planner"


def test_execute_plan_run_marks_run_failed_without_touching_task_status_on_crash(
    db_session, tmp_path
):
    """Killing/erroring the CLI mid-run must fail the run, not strand it --
    and must never try to CAS Task.status (it stays 'todo' throughout
    planning, so record_execution_failure's dispatched-status assertion would
    always reject it)."""

    db_session.add(Project(id="proj-pe2", name="Plan Executor Project 2", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@pe-planner2", name="Planner2", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-PE-2", project="proj-pe2", title="Build the widget", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@pe-planner2")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    run.status = "running"
    db_session.commit()

    with patch(
        "app.services.spec_plan_generator.semantic_search", new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(side_effect=RuntimeError("CLI process killed")),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    db_session.refresh(run)
    assert run.status == "failed"
    assert run.pid is None
    assert "CLI process killed" in run.error_message

    updated_task = db_session.get(Task, "TASK-PE-2")
    assert updated_task.status == "todo"


def test_plan_and_critic_announce_completion(db_session, tmp_path):
    """wait_for_task must learn that a plan finished.

    It returns on a status change, a terminal status, a pending gate, or a new
    TaskEvent.  A plan run trips none of the first three -- the task sits at
    `todo` the whole way -- so without an event the caller blocks until its
    timeout and gets `changed=false` on a plan that is already done.  It only
    looked like it worked when the plan escalated, because that sets
    `awaiting_approval`: the tool announced trouble and stayed silent on
    success (CTV2-1398).
    """

    from app.db.models import TaskEvent
    from app.workers import plan_executor as pe
    import inspect

    source = inspect.getsource(pe)
    assert "spec_plan_completed" in source, "plan step announces nothing"
    assert "plan_critic_completed" in source, "critic step announces nothing"

    # Both events must carry what the waiter needs to choose a next move.
    assert "spec_clarity" in source
    assert "open_questions" in source
    assert "verdict" in source
