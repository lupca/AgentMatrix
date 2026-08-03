import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, MagicMock, patch
from app.db.base import Base
from app.services.command_router import COMMANDS, HELP_COMMAND, CommandRouter
from app.services.graph_client import GraphClientError
from app.services.tool_registry import dump_registry

@pytest.fixture
def db_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()

def test_command_router_parse():
    router = CommandRouter(None)

    # Non-command message
    cmd, args = router.parse("hello world")
    assert cmd is None
    assert args == "hello world"

    # Slash command with arguments
    cmd, args = router.parse("/pm create new task")
    assert cmd == "create_task"
    assert args == "create new task"

    # Slash command without arguments
    cmd, args = router.parse("/help")
    assert cmd == "show_help"
    assert args == ""

    # Unknown command
    cmd, args = router.parse("/unknown_cmd arg")
    assert cmd is None
    assert args == "/unknown_cmd arg"


def test_command_key_truncation_preserves_attempt_discriminator():
    """Regression for CTV2-088 round 2: the 100-char cap used to be applied
    *after* appending `:{attempt}`, so a long session_id could chop off the
    attempt digit(s) and collide two different attempts onto the same key."""
    router = CommandRouter(None)
    long_session = "s" * 200

    key_attempt_1 = router._command_key(long_session, "dispatch", "args", attempt=1)
    key_attempt_2 = router._command_key(long_session, "dispatch", "args", attempt=2)

    assert len(key_attempt_1) <= 100
    assert len(key_attempt_2) <= 100
    assert key_attempt_1 != key_attempt_2
    assert key_attempt_1.endswith(":1")
    assert key_attempt_2.endswith(":2")

@pytest.mark.asyncio
async def test_command_router_execute():
    router = CommandRouter(None)
    res = await router.execute("show_help", "", "session-1")
    assert "commands" in res
    assert res["commands"] == dump_registry() + [HELP_COMMAND]
    aliases = {c["slash_alias"] for c in res["commands"]} - {None}
    assert aliases == set(COMMANDS.keys())

    res_unknown = await router.execute("non_existent_command", "", "session-1")
    assert "error" in res_unknown

@pytest.mark.asyncio
async def test_command_router_get_status(db_session):
    from app.db.models import Task, Project
    
    project = Project(id="proj-1", name="Test Project")
    db_session.add(project)
    
    task = Task(
        id="TASK-100",
        project="proj-1",
        title="Test Status Task",
        status="todo",
        current_gate="spec"
    )
    db_session.add(task)
    db_session.commit()
    
    router = CommandRouter(db_session)
    res = await router.execute("get_status", "TASK-100", "session-1")
    assert res.get("status") == "success"
    assert res.get("task", {}).get("id") == "TASK-100"
    assert res.get("task", {}).get("title") == "Test Status Task"

    res_list = await router.execute("get_status", "", "session-1")
    assert res_list.get("status") == "success"
    assert len(res_list.get("tasks", [])) > 0


@pytest.mark.asyncio
async def test_create_task_uses_explicit_project_and_prefix(db_session):
    """CTV2-092 AC: no more hardcoded project='default'; explicit --project
    is used, and the ID is derived from Project.task_prefix + a counter."""
    from app.db.models import Project

    db_session.add(Project(id="alpha", name="Alpha", task_prefix="ALPH"))
    db_session.commit()

    router = CommandRouter(db_session)
    res = await router.execute("create_task", "Do the thing --project alpha", "session-1")

    assert res["action"] == "created"
    assert res["project"] == "alpha"
    assert res["task_id"] == "ALPH-001"


@pytest.mark.asyncio
async def test_create_task_falls_back_to_session_project_scope(db_session):
    """create_task with no --project must resolve from the session's
    project scope instead of writing into a non-existent 'default' project."""
    from app.db.models import Project, Session as SessionModel

    db_session.add(Project(id="alpha", name="Alpha"))
    db_session.add(
        SessionModel(id="scoped-session", project_id="alpha", context_level="project")
    )
    db_session.commit()

    router = CommandRouter(db_session)
    res = await router.execute("create_task", "Do the thing", "scoped-session")

    assert res["action"] == "created"
    assert res["project"] == "alpha"


@pytest.mark.asyncio
async def test_create_task_without_resolvable_project_asks_user_instead_of_default(
    db_session,
):
    """A global session with no --project must not silently write into a
    'default' project (which doesn't exist and used to raise IntegrityError)."""
    router = CommandRouter(db_session)
    res = await router.execute("create_task", "Do the thing", "global-session-no-project")

    assert res["action"] == "error"
    assert res["error"] == "project_required"

    from app.db.models import Task

    assert db_session.query(Task).count() == 0


@pytest.mark.asyncio
async def test_create_task_unknown_project_returns_clear_error(db_session):
    router = CommandRouter(db_session)
    res = await router.execute(
        "create_task", "Do the thing --project ghost", "session-1"
    )

    assert res["action"] == "error"
    assert res["error"] == "unknown_project"


def test_create_task_concurrent_ids_are_unique_and_never_reused(tmp_path):
    """AC: 20 concurrent create_task calls against the same project must
    yield 20 unique IDs, and a COUNT(*)-based scheme's ID-reuse-after-delete
    bug must not resurface now that generation uses a persistent counter."""
    import asyncio
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models import Project, Task

    db_path = tmp_path / "task_ids.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"timeout": 30, "check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    setup = SessionFactory()
    setup.add(Project(id="race", name="Race Project", task_prefix="RACE"))
    setup.commit()
    setup.close()

    # Create then delete a task first: the counter must not reuse "RACE-001"
    # for anything created afterwards, unlike the old COUNT(*) scheme.
    seed_session = SessionFactory()
    seed_result = asyncio.run(
        CommandRouter(seed_session).execute(
            "create_task", "seed --project race", "seed-session"
        )
    )
    assert seed_result["task_id"] == "RACE-001"
    seed_session.query(Task).filter(Task.id == "RACE-001").delete()
    seed_session.commit()
    seed_session.close()

    barrier = threading.Barrier(20)
    results = []
    results_lock = threading.Lock()

    def attempt(index):
        session = SessionFactory()
        router = CommandRouter(session)
        try:
            barrier.wait(timeout=5)
            result = asyncio.run(
                router.execute(
                    "create_task", f"Task {index} --project race", f"session-{index}"
                )
            )
            with results_lock:
                results.append(result)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    task_ids = [r["task_id"] for r in results if r.get("action") == "created"]
    assert len(task_ids) == 20
    assert len(set(task_ids)) == 20
    assert "RACE-001" not in task_ids

    verify = SessionFactory()
    try:
        assert verify.query(Task).filter(Task.project == "race").count() == 20
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_command_router_persists_run_before_enqueueing(db_session):
    from app.db.models import Agent, AgentRun, Project, Task

    db_session.add(Project(id="proj-1", name="Test Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@agent-1",
            name="Agent 1",
            role="executor",
            cli="codex",
        )
    )
    db_session.add(
        Task(
            id="TASK-101",
            project="proj-1",
            title="Dispatch task",
            status="todo",
            current_gate="spec",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    queued_run_ids = []

    def assert_run_exists(run_id, *_args):
        queued_run_ids.append(run_id)
        run = db_session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == "queued"
        return MagicMock(message_id="msg-123")

    with patch("app.workers.agent_runner.run_agent.send", side_effect=assert_run_exists):
        result = await CommandRouter(db_session).execute(
            "dispatch_task", "TASK-101 @agent-1", "session-1"
        )

    assert result["action"] == "dispatched"
    assert queued_run_ids == [result["run_id"]]
    assert db_session.get(AgentRun, result["run_id"]).agent_id == "@agent-1"


@pytest.mark.asyncio
async def test_dispatch_task_parses_effort_flag(db_session):
    from app.db.models import Agent, AgentRun, Project, Task

    db_session.add(Project(id="proj-1", name="Test Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@agent-1",
            name="Agent 1",
            role="executor",
            cli="codex",
        )
    )
    db_session.add(
        Task(
            id="TASK-EFFORT",
            project="proj-1",
            title="Dispatch task",
            status="todo",
            current_gate="spec",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    with patch("app.workers.agent_runner.run_agent.send"):
        result = await CommandRouter(db_session).execute(
            "dispatch_task", "TASK-EFFORT @agent-1 --effort high", "session-1"
        )

    assert result["action"] == "dispatched"
    run = db_session.get(AgentRun, result["run_id"])
    assert run.effort == "high"
    assert "model_reasoning_effort=high" in run.command


@pytest.mark.asyncio
async def test_dispatch_retry_after_queue_failure_creates_new_run(db_session):
    """Regression for CTV2-088: `_command_key` used to hash session+action+args
    only, so a coordinator retry of `/dispatch` after a queue failure reused
    the exact same idempotency key. The stale ledger record (still `approved`,
    still pointing at the now-dead AgentRun) was returned as `applied=True`
    without ever creating a new run — a silent stuck dispatch. The attempt
    number in the key, plus the stale-record guard, must force a fresh run."""
    from app.db.models import Agent, AgentRun, Project, Task
    from app.services.task_orchestration import TaskOrchestrationService

    db_session.add(Project(id="proj-retry", name="Retry Project", repo_root="/tmp"))
    db_session.add(
        Agent(id="@agent-retry", name="Agent Retry", role="executor", cli="codex")
    )
    db_session.add(
        Task(
            id="TASK-RETRY",
            project="proj-retry",
            title="Retry task",
            status="todo",
            current_gate="spec",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    router = CommandRouter(db_session)

    with patch(
        "app.workers.agent_runner.run_agent.send",
        side_effect=RuntimeError("queue unavailable"),
    ):
        first = await router.execute("dispatch_task", "TASK-RETRY @agent-retry", "s-1")

    assert "error" in first
    first_run_id = first["run_id"]
    first_run = db_session.get(AgentRun, first_run_id)
    assert first_run.status == "failed"
    task = db_session.get(Task, "TASK-RETRY")
    assert task.status == "todo"  # queue-failure handling resets it

    with patch("app.workers.agent_runner.run_agent.send") as mock_send:
        second = await router.execute("dispatch_task", "TASK-RETRY @agent-retry", "s-1")

    assert second["action"] == "dispatched"
    assert second["run_id"] != first_run_id
    mock_send.assert_called_once()
    assert (
        db_session.query(AgentRun).filter(AgentRun.task_id == "TASK-RETRY").count()
        == 2
    )


@pytest.mark.asyncio
async def test_request_review_auto_selects_independent_reviewer_and_dispatches(
    db_session,
):
    from app.db.models import Agent, AgentRun, Project, Task

    db_session.add(Project(id="proj-rev", name="Review Project", repo_root="/tmp"))
    db_session.add(
        Agent(id="@executor-1", name="Executor", role="executor", cli="codex")
    )
    db_session.add(
        Agent(id="@reviewer-1", name="Reviewer", role="reviewer", cli="codex")
    )
    db_session.add(
        Task(
            id="TASK-REV",
            project="proj-rev",
            title="Task under review",
            status="awaiting-review",
            current_gate="review_order",
            mode="bypass",
            executor="@executor-1",
            result_ref="base-sha..head-sha",
        )
    )
    db_session.commit()

    with patch("app.workers.agent_runner.run_agent.send") as mock_send:
        result = await CommandRouter(db_session).execute(
            "request_review", "TASK-REV", "session-1"
        )

    assert result["action"] == "review_requested"
    assert result["reviewer"] == "@reviewer-1"
    mock_send.assert_called_once()
    task = db_session.get(Task, "TASK-REV")
    assert task.status == "in-review"
    assert task.reviewer == "@reviewer-1"
    run = db_session.query(AgentRun).filter(AgentRun.task_id == "TASK-REV").one()
    assert run.kind == "review"
    assert run.agent_id == "@reviewer-1"


@pytest.mark.asyncio
async def test_request_review_refuses_when_no_independent_reviewer_available(
    db_session,
):
    from app.db.models import Agent, Project, Task

    db_session.add(Project(id="proj-solo", name="Solo Project", repo_root="/tmp"))
    db_session.add(
        Agent(id="@only-agent", name="Only Agent", role="executor", cli="codex")
    )
    db_session.add(
        Task(
            id="TASK-SOLO",
            project="proj-solo",
            title="Task with a single available agent",
            status="awaiting-review",
            current_gate="review_order",
            mode="bypass",
            executor="@only-agent",
            result_ref="base-sha..head-sha",
        )
    )
    db_session.commit()

    result = await CommandRouter(db_session).execute(
        "request_review", "TASK-SOLO", "session-1"
    )

    assert result.get("reason") == "no_independent_reviewer"
    task = db_session.get(Task, "TASK-SOLO")
    assert task.status == "awaiting-review"  # never downgraded to a same-agent reviewer


@pytest.mark.asyncio
async def test_request_review_rejects_disabled_reviewer_with_valid_suggestions(db_session):
    from app.db.models import Agent, Project, Task

    db_session.add(Project(id="proj-disabled-review", name="Review", repo_root="/tmp"))
    db_session.add_all([
        Agent(id="@review-executor", name="Executor", role="executor", cli="codex"),
        Agent(
            id="@review-disabled",
            name="Disabled",
            role="reviewer",
            cli="codex",
            status="disabled",
        ),
        Agent(id="@review-valid-1", name="Valid 1", role="reviewer", cli="codex"),
        Agent(id="@review-valid-2", name="Valid 2", role="reviewer", cli="codex"),
    ])
    db_session.add(
        Task(
            id="TASK-REV-DISABLED",
            project="proj-disabled-review",
            title="Disabled reviewer",
            status="awaiting-review",
            mode="supervised",
            executor="@review-executor",
            result_ref="base..head",
        )
    )
    db_session.commit()

    result = await CommandRouter(db_session).execute_tool(
        "request_review",
        {"task_id": "TASK-REV-DISABLED", "reviewer": "@review-disabled"},
        "session-1",
    )

    assert "status 'disabled'" in result["error"]
    assert "Valid reviewer suggestions:" in result["error"]
    assert "@review-valid-1" in result["error"]
    assert "@review-valid-2" in result["error"]
    assert db_session.get(Task, "TASK-REV-DISABLED").reviewer is None


@pytest.mark.asyncio
async def test_explicit_valid_reviewer_survives_review_gate_approval(db_session):
    from app.db.models import Agent, GateRecord, Project, Task

    db_session.add(Project(id="proj-explicit-review", name="Review", repo_root="/tmp"))
    db_session.add_all([
        Agent(id="@explicit-executor", name="Executor", role="executor", cli="codex"),
        Agent(id="@explicit-reviewer", name="Reviewer", role="reviewer", cli="codex"),
    ])
    db_session.add(
        Task(
            id="TASK-REV-EXPLICIT",
            project="proj-explicit-review",
            title="Explicit reviewer",
            status="awaiting-review",
            mode="supervised",
            executor="@explicit-executor",
            result_ref="base..head",
        )
    )
    db_session.commit()
    router = CommandRouter(db_session)

    pending = await router.execute_tool(
        "request_review",
        {"task_id": "TASK-REV-EXPLICIT", "reviewer": "@explicit-reviewer"},
        "session-1",
    )
    gate = db_session.get(GateRecord, pending["gate_record_id"])
    reason = gate.input_payload["selection_reason"]
    assert gate.input_payload["reviewer"] == "@explicit-reviewer"
    assert "Reviewer đề xuất: @explicit-reviewer" in gate.input_payload["approval_prompt"]

    with (
        patch("app.workers.agent_runner.run_agent.send") as run_send,
        patch("app.workers.agent_runner.advance_task.send"),
    ):
        run_send.return_value = MagicMock(message_id="msg-explicit-review")
        approved = await router.execute_tool(
            "approve_gate",
            {"gate_record_id": gate.id, "decision": "approved"},
            "session-1",
        )

    assert approved["reviewer"] == "@explicit-reviewer"
    assert approved["selection_reason"] == reason
    task = db_session.get(Task, "TASK-REV-EXPLICIT")
    assert task.status == "in-review"
    assert task.reviewer == "@explicit-reviewer"


def test_concurrent_dispatch_with_same_idempotency_key_creates_one_run(tmp_path):
    """AC: two concurrent calls with identical args in the same cycle must
    only ever create a single AgentRun, relying on the existing DB
    idempotency-key uniqueness rather than widening any row lock."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models import Agent, AgentRun, Project, Task
    from app.services.task_orchestration import TaskOrchestrationService

    db_path = tmp_path / "race.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"timeout": 30, "check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    setup = SessionFactory()
    setup.add(Project(id="proj-race", name="Race Project", repo_root="/tmp"))
    setup.add(Agent(id="@agent-race", name="Agent Race", role="executor", cli="codex"))
    setup.add(
        Task(
            id="TASK-RACE",
            project="proj-race",
            title="Race task",
            status="todo",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def attempt():
        session = SessionFactory()
        try:
            barrier.wait(timeout=5)
            result = TaskOrchestrationService(session).request_dispatch(
                task_id="TASK-RACE",
                agent_id="@agent-race",
                actor="@operator",
                idempotency_key="race-key",
            )
            run_id = result.agent_run.id if result.agent_run else None
            with results_lock:
                results.append(("ok", run_id))
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, not raised
            with results_lock:
                results.append(("error", type(exc).__name__))
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    successful_run_ids = {run_id for kind, run_id in results if kind == "ok"}
    assert len(successful_run_ids) <= 1

    verify = SessionFactory()
    try:
        assert (
            verify.query(AgentRun).filter(AgentRun.task_id == "TASK-RACE").count() == 1
        )
    finally:
        verify.close()


def test_concurrent_dispatch(tmp_path):
    """AC (CTV2-204): two concurrent dispatch attempts with *different*
    idempotency keys -- the GateRecord idempotency-key uniqueness that
    protects same-key races doesn't apply here -- must still land exactly
    one AgentRun, guarded by Task.version compare-and-set."""
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models import Agent, AgentRun, Project, Task
    from app.services.task_orchestration import TaskOrchestrationService

    db_path = tmp_path / "race2.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"timeout": 30, "check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    setup = SessionFactory()
    setup.add(Project(id="proj-race2", name="Race Project 2", repo_root="/tmp"))
    setup.add(Agent(id="@agent-race2", name="Agent Race 2", role="executor", cli="codex"))
    setup.add(
        Task(
            id="TASK-RACE2",
            project="proj-race2",
            title="Race task 2",
            status="todo",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    setup.commit()
    setup.close()

    barrier = threading.Barrier(2)
    results = []
    results_lock = threading.Lock()

    def attempt(idempotency_key):
        session = SessionFactory()
        try:
            barrier.wait(timeout=5)
            result = TaskOrchestrationService(session).request_dispatch(
                task_id="TASK-RACE2",
                agent_id="@agent-race2",
                actor="@operator",
                idempotency_key=idempotency_key,
            )
            run_id = result.agent_run.id if result.agent_run else None
            with results_lock:
                results.append(("ok", run_id))
        except Exception as exc:  # noqa: BLE001 - any failure is recorded, not raised
            with results_lock:
                results.append(("error", type(exc).__name__))
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=(f"race2-key-{i}",)) for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(results) == 2
    successful_run_ids = {run_id for kind, run_id in results if kind == "ok"}
    assert len(successful_run_ids) == 1
    errors = [kind for kind, _ in results if kind == "error"]
    assert len(errors) == 1

    verify = SessionFactory()
    try:
        assert (
            verify.query(AgentRun).filter(AgentRun.task_id == "TASK-RACE2").count() == 1
        )
        task = verify.get(Task, "TASK-RACE2")
        assert task.status == "dispatched"
        assert task.version == 1
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_query_db_agents_never_includes_api_key(db_session):
    from app.db.models import Agent

    db_session.add(
        Agent(
            id="@agent-secret",
            name="Secret Agent",
            role="executor",
            agent_type="api",
            provider="openai",
            api_key="sk-super-secret",
        )
    )
    db_session.commit()

    result = await CommandRouter(db_session).execute("query_db", "agents", "session-1")

    assert result["status"] == "success"
    assert result["entity"] == "agents"
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["id"] == "@agent-secret"
    assert "api_key" not in row
    assert "sk-super-secret" not in str(row)


@pytest.mark.asyncio
async def test_query_db_unknown_entity_returns_clear_error(db_session):
    result = await CommandRouter(db_session).execute("query_db", "not_an_entity", "session-1")
    assert "error" in result
    assert "not_an_entity" in result["error"]
    assert "tasks" in result["error"]


@pytest.mark.asyncio
async def test_query_db_unknown_filter_returns_clear_error(db_session):
    result = await CommandRouter(db_session).execute(
        "query_db", "tasks bogus_field=1", "session-1"
    )
    assert "error" in result
    assert "bogus_field" in result["error"]


@pytest.mark.asyncio
async def test_query_db_filters_rows_and_caps_limit(db_session):
    from app.db.models import Project, Task

    db_session.add(Project(id="proj-1", name="Test Project"))
    for i in range(3):
        db_session.add(
            Task(
                id=f"TASK-2{i}0",
                project="proj-1",
                title=f"Task {i}",
                status="dispatched" if i < 2 else "todo",
            )
        )
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute("query_db", "tasks status=dispatched limit=1", "session-1")

    assert result["status"] == "success"
    assert result["limit"] == 1
    assert result["count"] == 1
    assert result["rows"][0]["status"] == "dispatched"

    bad_limit = await router.execute("query_db", "tasks limit=999", "session-1")
    assert "error" in bad_limit


@pytest.mark.asyncio
async def test_query_db_filters_tasks_by_exact_id(db_session):
    from app.db.models import Project, Task

    db_session.add(Project(id="proj-id-filter", name="ID Filter Project"))
    db_session.add_all(
        [
            Task(
                id=f"TASK-ID-{index}",
                project="proj-id-filter",
                title=f"Task {index}",
                status="todo",
            )
            for index in range(1, 4)
        ]
    )
    db_session.commit()

    router = CommandRouter(db_session)
    found = await router.execute_tool(
        "query_db",
        {"entity": "tasks", "filters": {"id": "TASK-ID-2"}},
        "session-1",
    )
    missing = await router.execute_tool(
        "query_db",
        {"entity": "tasks", "filters": {"id": "TASK-ID-404"}},
        "session-1",
    )

    assert found["count"] == 1
    assert found["rows"] == [
        {
            "id": "TASK-ID-2",
            "title": "Task 2",
            "status": "todo",
            "project": "proj-id-filter",
            "executor": None,
            "reviewer": None,
            "current_gate": "spec",
        }
    ]
    assert missing["status"] == "success"
    assert missing["count"] == 0
    assert missing["rows"] == []


@pytest.mark.asyncio
async def test_dispatch_supervised_returns_post_mutation_task_snapshot(db_session):
    from app.db.models import Agent, Task

    db_session.add(Agent(id="@snapshot-agent", name="Snapshot Agent", role="executor", cli="codex"))
    db_session.add(
        Task(
            id="SNAPSHOT-1",
            project="missing-snapshot-project",
            title="Snapshot task",
            status="todo",
            mode="supervised",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    with patch(
        "app.services.task_orchestration.build_dispatch_command",
        return_value=("codex exec task", "/tmp", "codex"),
    ):
        result = await CommandRouter(db_session).execute_tool(
            "dispatch_task",
            {"task_id": "SNAPSHOT-1", "executor": "@snapshot-agent"},
            "session-1",
        )

    task = db_session.get(Task, "SNAPSHOT-1")
    assert result["action"] == "dispatch_pending"
    assert result["task"] == {
        "id": task.id,
        "status": task.status,
        "current_gate": task.current_gate,
        "awaiting_approval": True,
        "approval_prompt": task.approval_prompt,
        "executor": task.executor,
        "reviewer": task.reviewer,
        "result_ref": task.result_ref,
        "landed_ref": task.landed_ref,
        "error": task.error,
        "spec_clarity": task.spec_clarity,
        "open_questions": [],
    }
    assert task.awaiting_approval is True


@pytest.mark.asyncio
async def test_get_status_reports_only_unresolved_pending_gate(db_session):
    from app.db.models import Agent, Task

    db_session.add(Agent(id="@status-agent", name="Status Agent", role="executor", cli="codex"))
    db_session.add(
        Task(
            id="STATUS-GATE-1",
            project="missing-status-project",
            title="Status gate task",
            status="todo",
            mode="supervised",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()
    router = CommandRouter(db_session)

    with patch(
        "app.services.task_orchestration.build_dispatch_command",
        return_value=("codex exec task", "/tmp", "codex"),
    ):
        pending = await router.execute_tool(
            "dispatch_task",
            {"task_id": "STATUS-GATE-1", "executor": "@status-agent"},
            "session-1",
        )

    before = await router.execute_tool(
        "get_status", {"task_id": "STATUS-GATE-1"}, "session-1"
    )
    assert before["task"]["awaiting_approval"] is True
    assert before["task"]["pending_gate"] == {
        "gate_record_id": pending["gate_record_id"],
        "gate_type": "dispatch",
        "created_at": before["task"]["pending_gate"]["created_at"],
    }
    assert before["task"]["pending_gate"]["created_at"] is not None

    with patch("app.workers.agent_runner.run_agent.send"):
        approval = await router.execute_tool(
            "approve_gate",
            {"gate_record_id": pending["gate_record_id"]},
            "session-1",
        )
    assert approval["decision"] == "approved"

    after = await router.execute_tool(
        "get_status", {"task_id": "STATUS-GATE-1"}, "session-1"
    )
    assert after["task"]["pending_gate"] is None
    assert after["task"]["awaiting_approval"] is False


@pytest.mark.asyncio
async def test_query_db_via_tool_call_matches_slash_command(db_session):
    from app.db.models import Project, Task

    db_session.add(Project(id="proj-1", name="Test Project"))
    db_session.add(Task(id="TASK-300", project="proj-1", title="Tool call task", status="todo"))
    db_session.commit()

    router = CommandRouter(db_session)
    tool_result = await router.execute_tool(
        "query_db",
        {"entity": "tasks", "filters": {"status": "todo"}, "limit": 5},
        "session-1",
    )

    assert tool_result["status"] == "success"
    assert tool_result["entity"] == "tasks"
    assert any(row["id"] == "TASK-300" for row in tool_result["rows"])


@pytest.mark.asyncio
async def test_manage_agent_api_key_is_encrypted_before_the_gate_ledger(db_session):
    """api_key is write-only: accepted, but only the ciphertext may ever be
    persisted — the admin-gate ledger is append-only, so a plaintext key
    stored there could never be redacted."""
    result = await CommandRouter(db_session).execute_tool(
        "manage_agent",
        {
            "action": "create",
            "id": "agent-x",
            "name": "X",
            "role": "executor",
            "agent_type": "api",
            "provider": "openai",
            "api_key": "sk-leak",
            "mode": "bypass",
        },
        "session-1",
    )
    assert "error" not in result, result

    import json as _json

    from app.db.models import AdminGateRecord, Agent

    agent = db_session.query(Agent).filter(Agent.id == "agent-x").first()
    assert agent is not None
    assert agent.api_key and agent.api_key != "sk-leak"
    assert "sk-leak" not in _json.dumps(result)
    for record in db_session.query(AdminGateRecord).all():
        assert "sk-leak" not in _json.dumps(record.input_payload or {})


@pytest.mark.asyncio
async def test_manage_project_bypass_applies_immediately_with_audit(db_session):
    from app.db.models import AuditLog, Project

    result = await CommandRouter(db_session).execute_tool(
        "manage_project",
        {"action": "create", "id": "proj-bypass", "name": "Bypass Project", "mode": "bypass"},
        "session-1",
    )

    assert result["action"] == "projects_created"
    assert result["id"] == "proj-bypass"
    project = db_session.query(Project).filter(Project.id == "proj-bypass").first()
    assert project is not None
    assert project.name == "Bypass Project"

    audit_rows = db_session.query(AuditLog).filter(AuditLog.action.like("admin_gate:%")).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].actor == "chat:session-1"


@pytest.mark.asyncio
async def test_manage_project_archive_supervised_pends_then_approves(db_session):
    from app.db.models import AuditLog, Project

    db_session.add(Project(id="proj-archive", name="To Archive", status="active"))
    db_session.commit()

    router = CommandRouter(db_session)
    pending = await router.execute_tool(
        "manage_project",
        {"action": "archive", "id": "proj-archive"},
        "session-1",
    )

    assert pending["action"] == "projects_pending"
    assert pending["status"] == "pending"
    gate_record_id = pending["admin_gate_record_id"]
    assert gate_record_id.startswith("admin:")

    # Not mutated yet.
    project = db_session.query(Project).filter(Project.id == "proj-archive").first()
    assert project.status == "active"

    approval = await router.execute_tool(
        "approve_gate",
        {"gate_record_id": gate_record_id},
        "session-2",
    )
    assert approval["action"] == "admin_gate_decision"
    assert approval["decision"] == "approved"
    assert approval["entity_id"] == "proj-archive"

    db_session.refresh(project)
    assert project.status == "archived"

    audit_rows = db_session.query(AuditLog).filter(AuditLog.action.like("admin_gate:%")).all()
    assert len(audit_rows) == 2
    assert {row.actor for row in audit_rows} == {"chat:session-1", "chat:session-2"}


@pytest.mark.asyncio
async def test_manage_agent_create_and_disable_bypass_no_hard_delete(db_session):
    from app.db.models import Agent

    router = CommandRouter(db_session)
    created = await router.execute_tool(
        "manage_agent",
        {
            "action": "create",
            "id": "agent-cli-1",
            "name": "CLI Agent",
            "role": "executor",
            "agent_type": "cli",
            "cli": "codex",
            "mode": "bypass",
        },
        "session-1",
    )
    assert created["action"] == "agents_created"
    assert "api_key" not in created

    disabled = await router.execute_tool(
        "manage_agent",
        {"action": "disable", "id": "agent-cli-1", "mode": "bypass"},
        "session-1",
    )
    assert disabled["action"] == "agents_disabled"
    assert disabled["status"] == "disabled"

    agent = db_session.query(Agent).filter(Agent.id == "agent-cli-1").first()
    assert agent is not None
    assert agent.status == "disabled"


@pytest.mark.asyncio
async def test_manage_knowledge_create_update_archive(db_session):
    router = CommandRouter(db_session)

    created = await router.execute_tool(
        "manage_knowledge",
        {"action": "create", "title": "Runbook", "category": "ops"},
        "session-1",
    )
    assert created["action"] == "knowledge_created"
    item_id = created["id"]

    updated = await router.execute_tool(
        "manage_knowledge",
        {"action": "update", "id": item_id, "category": "final"},
        "session-1",
    )
    assert updated["action"] == "knowledge_updated"

    archived = await router.execute_tool(
        "manage_knowledge",
        {"action": "archive", "id": item_id},
        "session-1",
    )
    assert archived["action"] == "knowledge_archived"
    assert archived["status"] == "archived"


@pytest.mark.asyncio
async def test_update_settings_bypass_applies_immediately_with_audit(db_session):
    from app.db.models import AuditLog, Setting

    result = await CommandRouter(db_session).execute_tool(
        "update_settings",
        {"key": "default_mode", "value": "bypass", "mode": "bypass"},
        "session-1",
    )

    assert result["action"] == "settings_updated"
    assert result["key"] == "default_mode"
    assert result["value"] == "bypass"

    setting = db_session.query(Setting).filter(Setting.key == "default_mode").first()
    assert setting is not None
    assert setting.value == "bypass"

    audit_rows = db_session.query(AuditLog).filter(AuditLog.action.like("admin_gate:%")).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].actor == "chat:session-1"


@pytest.mark.asyncio
async def test_update_settings_supervised_pends_then_approves(db_session):
    from app.db.models import Setting

    router = CommandRouter(db_session)
    pending = await router.execute_tool(
        "update_settings",
        {"key": "context_snapshot_top_n", "value": 10},
        "session-1",
    )

    assert pending["action"] == "settings_pending"
    assert pending["status"] == "pending"
    gate_record_id = pending["admin_gate_record_id"]
    assert gate_record_id.startswith("admin:")

    # Not written yet.
    assert db_session.query(Setting).filter(Setting.key == "context_snapshot_top_n").first() is None

    approval = await router.execute_tool(
        "approve_gate",
        {"gate_record_id": gate_record_id},
        "session-2",
    )
    assert approval["action"] == "admin_gate_decision"
    assert approval["decision"] == "approved"
    assert approval["entity_id"] == "context_snapshot_top_n"

    setting = db_session.query(Setting).filter(Setting.key == "context_snapshot_top_n").first()
    assert setting is not None
    assert setting.value == 10


@pytest.mark.asyncio
async def test_update_settings_rejects_key_outside_whitelist_no_db_write(db_session):
    from app.db.models import AdminGateRecord, Setting

    result = await CommandRouter(db_session).execute_tool(
        "update_settings",
        {"key": "bogus_key", "value": "anything"},
        "session-1",
    )

    assert "error" in result
    assert "bogus_key" in result["error"]
    assert db_session.query(Setting).count() == 0
    assert db_session.query(AdminGateRecord).count() == 0


@pytest.mark.asyncio
async def test_query_db_settings_reads_whitelisted_values(db_session):
    from app.db.models import Setting

    db_session.add(Setting(key="default_mode", value="supervised", description="d"))
    db_session.commit()

    result = await CommandRouter(db_session).execute("query_db", "settings", "session-1")

    assert result["status"] == "success"
    assert result["entity"] == "settings"
    assert result["rows"] == [
        {"key": "default_mode", "value": "supervised", "description": "d"}
    ]


@pytest.mark.asyncio
async def test_update_task_edits_plan_and_rejects_status(db_session):
    from app.db.models import Project, Task

    db_session.add(Project(id="proj-1", name="Test Project"))
    db_session.add(Task(id="TASK-400", project="proj-1", title="Patchable", status="todo"))
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "update_task",
        {
            "task_id": "TASK-400",
            "patch": {
                "plan": "Do the thing",
                "priority": "high",
                "raw_input": "Answers replace the previous task description.",
            },
        },
        "session-1",
    )
    assert result["action"] == "updated"
    assert result["plan"] == "Do the thing"
    assert result["priority"] == "high"
    assert result["raw_input"] == "Answers replace the previous task description."

    task = db_session.query(Task).filter(Task.id == "TASK-400").first()
    assert task.status == "todo"
    assert task.raw_input == "Answers replace the previous task description."

    rejected = await router.execute_tool(
        "update_task",
        {"task_id": "TASK-400", "patch": {"status": "done"}},
        "session-1",
    )
    assert "error" in rejected


@pytest.mark.asyncio
async def test_get_impact_radius_requires_project_scope(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "get_impact_radius", {"file": "src/index.ts"}, "session-unscoped"
    )
    assert result["status"] == "error"
    assert result["reason"] == "research_requires_project_scope"


@pytest.mark.asyncio
async def test_get_impact_radius_project_without_repo_root_returns_structured_error(db_session):
    from app.db.models import Project, Session as SessionModel

    db_session.add(Project(id="proj-no-root", name="No Root Project"))
    db_session.add(SessionModel(id="session-1", project_id="proj-no-root", context_level="project"))
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "get_impact_radius", {"file": "src/index.ts"}, "session-1"
    )
    assert result["status"] == "error"
    assert result["reason"] == "project_repo_root_not_configured"
    assert result["project_id"] == "proj-no-root"


@pytest.mark.asyncio
async def test_get_impact_radius_resolves_repo_root_from_task_project(db_session, tmp_path):
    from app.db.models import Project, Task, Session as SessionModel

    repo_root = str(tmp_path)
    db_session.add(Project(id="proj-graph", name="Graph Project", repo_root=repo_root))
    db_session.add(Task(id="TASK-500", project="proj-graph", title="Touches graph", status="todo"))
    db_session.add(SessionModel(id="session-1", task_id="TASK-500", project_id="proj-graph", context_level="task"))
    db_session.commit()

    router = CommandRouter(db_session)
    with patch(
        "app.services.command_router.graph_get_impact_radius",
        new=AsyncMock(return_value=["a.py", "b.py"]),
    ) as mock_impact:
        result = await router.execute_tool(
            "get_impact_radius", {"file": "src/index.ts"}, "session-1"
        )

    assert result == {"status": "success", "repo_root": repo_root, "files": ["a.py", "b.py"]}
    mock_impact.assert_awaited_once_with(
        repo_root,
        "src/index.ts",
        max_depth=2,
        raise_on_error=True,
        compress_output=True,
    )


@pytest.mark.asyncio
async def test_get_impact_radius_forwards_requested_max_depth(db_session, tmp_path):
    from app.db.models import Project, Session as SessionModel

    repo_root = str(tmp_path)
    db_session.add(Project(id="proj-depth", name="Depth Project", repo_root=repo_root))
    db_session.add(SessionModel(
        id="session-depth", project_id="proj-depth", context_level="project"
    ))
    db_session.commit()

    router = CommandRouter(db_session)
    with patch(
        "app.services.command_router.graph_get_impact_radius",
        new=AsyncMock(return_value=["consumer.py"]),
    ) as mock_impact:
        result = await router.execute_tool(
            "get_impact_radius", {"file": "hub.py", "max_depth": 5}, "session-depth"
        )

    assert result["status"] == "success"
    mock_impact.assert_awaited_once_with(
        repo_root,
        "hub.py",
        max_depth=5,
        raise_on_error=True,
        compress_output=True,
    )


@pytest.mark.asyncio
async def test_lazy_graph_staleness_check_returns_result_and_warning_without_blocking(db_session, tmp_path):
    from app.db.models import OutboxEvent, Project, Session as SessionModel

    repo_root = str(tmp_path)
    project = Project(id="proj-stale-test", name="Stale Test", repo_root=repo_root, graph_status="idle")
    db_session.add(project)
    db_session.add(SessionModel(id="session-stale", project_id="proj-stale-test", context_level="project"))
    db_session.commit()

    router = CommandRouter(db_session)
    stale_payload = {
        "is_stale": True,
        "built_at_sha": "sha123",
        "head_sha": "sha456",
        "warning": "graph đang cũ tại sha123",
    }
    with patch("app.services.command_router.graph_get_impact_radius", new=AsyncMock(return_value=["res.py"])), \
         patch("app.services.graph_client.check_graph_staleness", new=AsyncMock(return_value=stale_payload)):
        result = await router.execute_tool(
            "get_impact_radius", {"file": "src/index.ts"}, "session-stale"
        )

    # Tool returns normal results + warning + graph_stale flag without blocking
    assert result["status"] == "success"
    assert result["files"] == ["res.py"]
    assert result["graph_stale"] is True
    assert result["warning"] == "graph đang cũ tại sha123"

    db_session.refresh(project)
    assert project.graph_status == "stale"

    outbox_events = db_session.query(OutboxEvent).filter(OutboxEvent.event_type == "graph_rebuild_requested").all()
    assert len(outbox_events) == 1
    assert outbox_events[0].payload["project_id"] == "proj-stale-test"



@pytest.mark.asyncio
async def test_get_impact_radius_transport_error_returns_structured_error(db_session, tmp_path):
    from app.db.models import Project, Session as SessionModel

    repo_root = str(tmp_path)
    db_session.add(Project(id="proj-graph", name="Graph Project", repo_root=repo_root))
    db_session.add(SessionModel(id="session-1", project_id="proj-graph", context_level="project"))
    db_session.commit()

    router = CommandRouter(db_session)
    with patch(
        "app.services.command_router.graph_get_impact_radius",
        new=AsyncMock(
            side_effect=GraphClientError(
                "Graph MCP transport error: response exceeded stdio buffer limit",
                kind="transport",
            )
        ),
    ):
        result = await router.execute_tool(
            "get_impact_radius", {"file": "src/index.ts"}, "session-1"
        )

    assert result["status"] == "error"
    assert result["reason"] == "graph_transport_error"
    assert "stdio buffer limit" in result["detail"]
    assert result != []


@pytest.mark.parametrize(
    ("kind", "expected_reason", "message"),
    [
        ("graph_not_built", "graph_not_built", "Code graph has not been built"),
        (
            "transport",
            "graph_transport_error",
            "Graph MCP transport error: response exceeded stdio buffer limit",
        ),
    ],
)
def test_research_error_distinguishes_graph_state_from_transport(
    kind, expected_reason, message
):
    from app.services.command_router_handlers.context_handlers import (
        ContextHandlersMixin,
    )

    result = ContextHandlersMixin._research_error(GraphClientError(message, kind=kind))

    assert result["reason"] == expected_reason
    assert result["detail"] == message


@pytest.mark.asyncio
async def test_get_minimal_context_requires_project_scope(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "get_minimal_context", {"query": "how does dispatch work"}, "session-unscoped"
    )
    assert result["status"] == "error"
    assert result["reason"] == "research_requires_project_scope"


@pytest.mark.asyncio
async def test_get_minimal_context_missing_query_returns_clear_error(db_session):
    router = CommandRouter(db_session)
    result = await router.execute_tool("get_minimal_context", {}, "session-1")
    assert result == {"error": "query is required"}


@pytest.mark.asyncio
async def test_get_minimal_context_resolves_repo_root_and_compresses(db_session, tmp_path):
    from app.db.models import Project, Session as SessionModel

    repo_root = str(tmp_path)
    db_session.add(Project(id="proj-graph", name="Graph Project", repo_root=repo_root))
    db_session.add(SessionModel(id="session-1", project_id="proj-graph", context_level="project"))
    db_session.commit()

    router = CommandRouter(db_session)
    with patch(
        "app.services.command_router.semantic_search",
        new=AsyncMock(return_value="[compressed context]"),
    ) as mock_search:
        result = await router.execute_tool(
            "get_minimal_context",
            {"query": "how does dispatch work", "limit": 5},
            "session-1",
        )

    assert result == {
        "status": "success",
        "repo_root": repo_root,
        "context": "[compressed context]",
    }
    mock_search.assert_awaited_once_with(
        repo_root, "how does dispatch work", 5, raise_on_error=True, compress_output=True
    )


@pytest.mark.asyncio
async def test_get_minimal_context_graph_unavailable_returns_structured_error(db_session, tmp_path):
    from app.db.models import Project, Session as SessionModel

    repo_root = str(tmp_path)
    db_session.add(Project(id="proj-graph", name="Graph Project", repo_root=repo_root))
    db_session.add(SessionModel(id="session-1", project_id="proj-graph", context_level="project"))
    db_session.commit()

    router = CommandRouter(db_session)
    with patch(
        "app.services.command_router.semantic_search",
        new=AsyncMock(side_effect=GraphClientError("MCP timed out")),
    ):
        result = await router.execute_tool(
            "get_minimal_context", {"query": "how does dispatch work"}, "session-1"
        )

    assert result["status"] == "error"
    assert result["reason"] == "graph_unavailable"
    assert "MCP timed out" in result["detail"]


@pytest.mark.asyncio
async def test_generate_spec_plan_writes_result_and_opens_dispatch(db_session):
    """CTV2-091: /spec-plan runs the (mocked) LLM+graph generator and writes
    the result onto the task via TaskOrchestrationService, which is the only
    thing that lets a subsequent dispatch through.

    CTV2-109: no spec_plan_model gate anymore -- generation runs immediately
    with an explicitly passed agent_id."""
    from app.db.models import Agent, Project, Task
    from app.schemas.task import SpecPlanResult

    db_session.add(Project(id="proj-spec", name="Spec Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@spec-agent",
            name="Spec Agent",
            role="coordinator",
            cli="claude",
            capabilities=["coordinator"],
        )
    )
    db_session.add(
        Task(
            id="TASK-SPEC",
            project="proj-spec",
            title="Needs a spec",
            status="todo",
            acceptance_criteria=[],
            spec_clarity="low",
            open_questions=["Old unanswered question?"],
            awaiting_approval=True,
            approval_prompt="Old prompt",
        )
    )
    db_session.commit()

    fake_result = SpecPlanResult(
        schema_version="1.1",
        acceptance_criteria=["Does the thing"],
        plan="Do the thing.",
        files=["backend/app/thing.py"],
        tests=["backend/tests/test_thing.py"],
        risk="low",
        spec_clarity="high",
        open_questions=[],
    )

    with patch(
        "app.services.spec_plan_generator.generate_spec_plan",
        new=AsyncMock(return_value=(fake_result, ["thing-flow"])),
    ), patch("app.services.tool_metrics.record_tool_metric") as mock_metric:
        result = await CommandRouter(db_session).execute(
            "generate_spec_plan", "TASK-SPEC @spec-agent", "session-1"
        )

    assert result["action"] == "spec_plan_generated"
    assert result["acceptance_criteria"] == ["Does the thing"]
    assert result["flows"] == ["thing-flow"]

    task = db_session.get(Task, "TASK-SPEC")
    assert task.acceptance_criteria == ["Does the thing"]
    assert task.current_gate == "plan"
    assert task.open_questions == []
    assert task.spec_clarity == "high"
    assert task.awaiting_approval is False
    assert task.approval_prompt is None
    mock_metric.assert_called_once_with(
        tool="spec_plan",
        source="spec_plan_generator",
        ok=True,
        task_id="TASK-SPEC",
        result_count=0,
        payload={"spec_clarity": "high", "task_id": "TASK-SPEC"},
    )


@pytest.mark.asyncio
async def test_generate_spec_plan_returns_questions_and_escalates(db_session):
    from app.db.models import Agent, Project, Task
    from app.schemas.task import SpecPlanResult

    db_session.add(Project(id="proj-questions", name="Questions", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@question-planner",
            name="Question Planner",
            role="coordinator",
            cli="codex",
            capabilities=["coordinator"],
        )
    )
    db_session.add(
        Task(
            id="TASK-QUESTIONS",
            project="proj-questions",
            title="Needs clarification",
            status="todo",
            acceptance_criteria=[],
        )
    )
    db_session.commit()

    fake_result = SpecPlanResult(
        schema_version="1.1",
        acceptance_criteria=["Authentication behavior is covered by tests"],
        plan="Confirm auth convention, then implement.",
        files=["backend/app/auth.py"],
        tests=["backend/tests/test_auth.py"],
        risk="medium",
        spec_clarity="medium",
        open_questions=[
            "Which existing authentication convention should this use?",
            "Should anonymous callers receive 401 or 403?",
        ],
    )

    with patch(
        "app.services.spec_plan_generator.generate_spec_plan",
        new=AsyncMock(return_value=(fake_result, [])),
    ):
        result = await CommandRouter(db_session).execute_tool(
            "generate_spec_plan",
            {"task_id": "TASK-QUESTIONS", "agent_id": "@question-planner"},
            "session-questions",
        )

    assert result["action"] == "spec_questions_pending"
    assert result["spec_clarity"] == "medium"
    assert result["open_questions"] == fake_result.open_questions
    assert result["awaiting_approval"] is True
    assert "1) Which existing authentication convention should this use?" in result[
        "approval_prompt"
    ]
    assert "2) Should anonymous callers receive 401 or 403?" in result[
        "approval_prompt"
    ]

    task = db_session.get(Task, "TASK-QUESTIONS")
    assert task.open_questions == fake_result.open_questions
    assert task.spec_clarity == "medium"
    assert task.awaiting_approval is True


@pytest.mark.asyncio
async def test_generate_spec_plan_auto_suggests_agent_when_not_provided(db_session):
    """CTV2-109: with no agent_id argument, /spec-plan auto-selects a
    capable agent via AgentSuggester(role="spec_plan") instead of gating on
    a human-approved model choice."""
    from app.db.models import Agent, Project, Task
    from app.schemas.task import SpecPlanResult

    db_session.add(Project(id="proj-spec-auto", name="Auto Spec Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@auto-spec-agent",
            name="Auto Spec Agent",
            role="coordinator",
            cli="claude",
            capabilities=["coordinator"],
        )
    )
    db_session.add(
        Agent(
            id="@plain-executor",
            name="Plain Executor",
            role="executor",
            cli="codex",
            capabilities=["python"],
        )
    )
    db_session.add(
        Task(
            id="TASK-SPEC-AUTO",
            project="proj-spec-auto",
            title="Needs a spec",
            status="todo",
            acceptance_criteria=[],
        )
    )
    db_session.commit()

    fake_result = SpecPlanResult(
        schema_version="1.1",
        acceptance_criteria=["Does the thing"],
        plan="Do the thing.",
        files=[],
        tests=[],
        risk="low",
        spec_clarity="high",
        open_questions=[],
    )

    with patch(
        "app.services.spec_plan_generator.generate_spec_plan",
        new=AsyncMock(return_value=(fake_result, [])),
    ) as mock_generate:
        result = await CommandRouter(db_session).execute(
            "generate_spec_plan", "TASK-SPEC-AUTO", "session-1"
        )

    assert result["action"] == "spec_plan_generated"
    used_agent = mock_generate.call_args.args[2]
    assert used_agent.id == "@auto-spec-agent"
    assert mock_generate.call_args.args[1] == "/tmp"


@pytest.mark.asyncio
async def test_generate_spec_plan_errors_when_no_suitable_agent_found(db_session):
    """CTV2-109: no capable agent configured -> a clear error, no LLM call,
    and no fallback to an unconfigured provider."""
    from app.db.models import Agent, Project, Task

    db_session.add(Project(id="proj-spec-none", name="No Agent Spec Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@plain-executor-2",
            name="Plain Executor",
            role="executor",
            cli="codex",
            capabilities=["python"],
        )
    )
    db_session.add(
        Task(
            id="TASK-SPEC-NONE",
            project="proj-spec-none",
            title="Needs a spec",
            status="todo",
            acceptance_criteria=[],
        )
    )
    db_session.commit()

    with patch(
        "app.services.spec_plan_generator.generate_spec_plan",
        new=AsyncMock(side_effect=AssertionError("LLM must not be called with no suitable agent")),
    ):
        result = await CommandRouter(db_session).execute(
            "generate_spec_plan", "TASK-SPEC-NONE", "session-1"
        )

    assert "error" in result

    task = db_session.get(Task, "TASK-SPEC-NONE")
    assert task.acceptance_criteria == []


@pytest.mark.asyncio
async def test_generate_spec_plan_missing_task_returns_error(db_session):
    result = await CommandRouter(db_session).execute(
        "generate_spec_plan", "NOPE", "session-1"
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_record_verdict_loads_from_review_file(db_session, tmp_path):
    """Test record_verdict loading ac_results from review file."""
    import json
    from app.db.models import Project, Task
    from app.services.command_router import CommandRouter

    # Setup project with repo_root
    project = Project(id="test-verdict-proj", name="Test", repo_root=str(tmp_path))
    db_session.add(project)

    # Setup task
    task = Task(
        id="TEST-V001",
        project="test-verdict-proj",
        title="Test task",
        status="in-review",
        mode="bypass",
    )
    db_session.add(task)
    db_session.commit()

    # Create review file
    ct_dir = tmp_path / ".ct"
    ct_dir.mkdir()
    review_file = ct_dir / "review-TEST-V001.json"
    review_file.write_text(json.dumps({
        "schema_version": "1.0",
        "task_id": "TEST-V001",
        "base": "abc123",
        "head": "def456",
        "ac_results": [{"criterion_id": "ac-1", "status": "pass", "evidence": [], "finding_ids": []}],
        "findings": [],
        "tests_run": [],
        "tests_passed": [],
    }))

    router = CommandRouter(db_session)
    result = await router._handle_verdict("TEST-V001 pass", "test-session")

    # Should fail at orchestration level (no review run) but NOT at file loading
    assert "Review file not found" not in result.get("error", "")
    assert "Failed to load review file" not in result.get("error", "")
