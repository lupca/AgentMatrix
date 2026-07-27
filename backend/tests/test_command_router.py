import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch
from app.db.base import Base
from app.services.command_router import COMMANDS, HELP_COMMAND, CommandRouter
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
        )
    )
    db_session.commit()

    queued_run_ids = []

    def assert_run_exists(run_id, *_args):
        queued_run_ids.append(run_id)
        run = db_session.get(AgentRun, run_id)
        assert run is not None
        assert run.status == "queued"

    with patch("app.workers.agent_runner.run_agent.send", side_effect=assert_run_exists):
        result = await CommandRouter(db_session).execute(
            "dispatch_task", "TASK-101 @agent-1", "session-1"
        )

    assert result["action"] == "dispatched"
    assert queued_run_ids == [result["run_id"]]
    assert db_session.get(AgentRun, result["run_id"]).agent_id == "@agent-1"


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
async def test_manage_agent_rejects_payload_with_api_key(db_session):
    result = await CommandRouter(db_session).execute_tool(
        "manage_agent",
        {"action": "create", "id": "agent-x", "name": "X", "role": "executor", "api_key": "sk-leak"},
        "session-1",
    )
    assert "error" in result
    assert "api_key" in result["error"]

    from app.db.models import Agent

    assert db_session.query(Agent).filter(Agent.id == "agent-x").first() is None


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
async def test_update_task_edits_plan_and_rejects_status(db_session):
    from app.db.models import Project, Task

    db_session.add(Project(id="proj-1", name="Test Project"))
    db_session.add(Task(id="TASK-400", project="proj-1", title="Patchable", status="todo"))
    db_session.commit()

    router = CommandRouter(db_session)
    result = await router.execute_tool(
        "update_task",
        {"task_id": "TASK-400", "patch": {"plan": "Do the thing", "priority": "high"}},
        "session-1",
    )
    assert result["action"] == "updated"
    assert result["plan"] == "Do the thing"
    assert result["priority"] == "high"

    task = db_session.query(Task).filter(Task.id == "TASK-400").first()
    assert task.status == "todo"

    rejected = await router.execute_tool(
        "update_task",
        {"task_id": "TASK-400", "patch": {"status": "done"}},
        "session-1",
    )
    assert "error" in rejected
