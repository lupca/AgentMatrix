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

def test_plan_step_releases_transaction_before_semantic_search(db_session, tmp_path):
    """CTV2-1389: the read transaction opened by db.get(Agent,...) and
    build_project_context must be committed before asyncio.run enters
    generate_spec_plan -- semantic_search shells out to code-review-graph
    and must not inherit an idle-in-transaction session."""

    db_session.add(Project(id="proj-tx1", name="TX Project", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@tx-planner", name="Planner", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-TX-1", project="proj-tx1", title="TX test", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@tx-planner")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    run.status = "running"
    db_session.commit()

    transaction_state_during_search = {}

    async def capture_transaction_state(*args, **kwargs):
        transaction_state_during_search["in_transaction"] = db_session.in_transaction()
        return []

    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(side_effect=capture_transaction_state),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(_valid_plan_payload())),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    assert transaction_state_during_search.get("in_transaction") is False, (
        "semantic_search was called with an open DB transaction; "
        "the boundary commit before asyncio.run is missing or ineffective"
    )

    db_session.refresh(run)
    assert run.status == "success"

def test_critic_step_releases_transaction_before_llm_call(db_session, tmp_path):
    """CTV2-1389: same boundary rule for the critic step -- the reads for
    planner_agent, critic_agent, and build_project_context must be committed
    before asyncio.run enters criticize_spec_plan."""

    db_session.add(Project(id="proj-tx2", name="TX Project 2", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@tx-planner2", name="Planner2", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    db_session.add(
        Agent(id="@tx-critic2", name="Critic2", role="reviewer", cli="claude", capabilities=["reviewer"])
    )
    task = Task(
        id="TASK-TX-2",
        project="proj-tx2",
        title="Critic TX test",
        status="todo",
        planner="@tx-planner2",
        plan="Build widget.",
        acceptance_criteria=["Widget renders"],
        evidence=[{
            "fact": "Widget module exists", "source_type": "file",
            "source": "backend/app/widget.py:1", "result": "module exists",
        }],
        risk="low",
        spec_clarity="high",
    )
    db_session.add(task)
    db_session.commit()

    critic_agent = db_session.get(Agent, "@tx-critic2")
    run = plan_executor.create_plan_run(db_session, task, agent=critic_agent, step="critic")
    run.status = "running"
    db_session.commit()

    transaction_state_during_llm = {}

    def fake_complete(agent, messages, *args, **kwargs):
        transaction_state_during_llm["in_transaction"] = db_session.in_transaction()
        on_start = kwargs.get("on_start")
        if on_start is not None:
            on_start(123456)
        return _response({
            "schema_version": "1.0",
            "verdict": "accept",
            "findings": [],
            "summary": "Plan is sound.",
        })

    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(side_effect=fake_complete),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    assert transaction_state_during_llm.get("in_transaction") is False, (
        "critic LLM call was entered with an open DB transaction; "
        "the boundary commit before asyncio.run is missing or ineffective"
    )

    db_session.refresh(run)
    assert run.status == "success"

def test_planner_persists_task_after_boundary_commit_expires_orm_objects(
    db_session, tmp_path
):
    """CTV2-1389: after the boundary commit, expire_on_commit=True invalidates
    all cached ORM state.  The planner must still write the plan to the task
    and mark the run successful -- it must refresh/reload objects rather than
    relying on stale in-memory attributes."""

    db_session.add(Project(id="proj-tx3", name="TX Project 3", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@tx-planner3", name="Planner3", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-TX-3", project="proj-tx3", title="Expire test", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@tx-planner3")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    run.status = "running"
    db_session.commit()

    with patch(
        "app.services.spec_plan_generator.semantic_search", new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(_valid_plan_payload(
            plan="Refreshed plan after expiry.",
            acceptance_criteria=["Fresh criterion"],
        ))),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    db_session.refresh(run)
    assert run.status == "success"

    updated_task = db_session.get(Task, "TASK-TX-3")
    assert updated_task.plan == "Refreshed plan after expiry."
    assert updated_task.planner == "@tx-planner3"
    assert "Fresh criterion" in (updated_task.acceptance_criteria or [])


def test_plan_step_hands_the_run_deadline_to_the_llm_call(db_session, tmp_path):
    """CTV2-1410: the run's own timeout must reach the process that honours it.

    `execute_plan_run` has always received `timeout_seconds` and always
    dropped it, so the planner/critic CLI ran under `CLIDispatcher`'s 4-hour
    default. Measured 2026-08-06: a critic child alive 1h51m, zero output,
    while its row said 900s.
    """
    db_session.add(Project(id="proj-to1", name="Timeout Project", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@to-planner", name="Planner", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-TO-1", project="proj-to1", title="Timeout test", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@to-planner")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    run.status = "running"
    db_session.commit()

    seen: dict[str, object] = {}

    async def capture(agent_arg, messages, *args, **kwargs):
        seen["timeout_seconds"] = kwargs.get("timeout_seconds")
        seen["on_heartbeat"] = kwargs.get("on_heartbeat")
        return _response(_valid_plan_payload())

    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(side_effect=capture),
    ):
        plan_executor.execute_plan_run(db_session, run, task, 900)

    assert seen["timeout_seconds"] == 900
    # A run that reports nothing for an hour must still look alive-or-not in
    # the DB; without a heartbeat `updated_at` is frozen at started_at.
    assert callable(seen["on_heartbeat"])


def test_heartbeat_callback_moves_the_run_updated_at(db_session, tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.services import spec_plan_generator

    db_session.add(Project(id="proj-hb", name="HB Project", repo_root=str(tmp_path)))
    db_session.add(
        Agent(id="@hb-planner", name="Planner", role="coordinator", cli="claude", capabilities=["coordinator"])
    )
    task = Task(id="TASK-HB-1", project="proj-hb", title="HB test", status="todo")
    db_session.add(task)
    db_session.commit()

    agent = db_session.get(Agent, "@hb-planner")
    run = plan_executor.create_plan_run(db_session, task, agent=agent, step="plan")
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    run.status = "running"
    run.started_at = stale
    run.updated_at = stale
    db_session.commit()

    spec_plan_generator._heartbeat_recorder(db_session, run)(4242)

    db_session.refresh(run)
    updated_at = run.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    assert updated_at > stale
