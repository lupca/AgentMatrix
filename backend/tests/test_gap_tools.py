"""Tests for the B1.5b gap tools: get_task_events, archive_task,
suggest_agents, query_db entities agent_runs/audit, knowledge content on
point lookup, update_task dependency edits, and the manage_agent api_key
write-only path (pre-encrypted before the admin-gate ledger)."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Agent,
    AgentRun,
    AuditLog,
    KnowledgeItem,
    Project,
    Task,
    TaskDependency,
    TaskEvent,
)
from app.services.command_router import CommandRouter
from app.services.crypto import decrypt_api_key
from app.db.base import Base


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def seeded(db_session):
    db_session.add(Project(id="p1", name="Project One", repo_root="/tmp"))
    db_session.add(
        Task(id="T-1", title="Task one", project="p1", status="todo")
    )
    db_session.add(
        Task(id="T-2", title="Task two", project="p1", status="todo")
    )
    db_session.commit()
    return db_session


@pytest.mark.asyncio
async def test_get_task_events_cursor(seeded):
    for n in range(3):
        seeded.add(
            TaskEvent(task_id="T-1", event_type="status_changed", kind="info", payload={"n": n})
        )
    seeded.add(TaskEvent(task_id="T-2", event_type="gate_pending", kind="decision", payload={}))
    seeded.commit()

    router = CommandRouter(seeded)
    result = await router.execute_tool("get_task_events", {"task_id": "T-1"}, "s1")
    assert len(result["events"]) == 3
    cursor = result["cursor"]

    # Nothing new after the cursor.
    result = await router.execute_tool(
        "get_task_events", {"task_id": "T-1", "since_id": cursor}, "s1"
    )
    assert result["events"] == []
    assert result["cursor"] == cursor

    # kind filter crosses tasks.
    result = await router.execute_tool("get_task_events", {"kind": "decision"}, "s1")
    assert [e["task_id"] for e in result["events"]] == ["T-2"]


@pytest.mark.asyncio
async def test_archive_and_restore_task(seeded):
    router = CommandRouter(seeded)
    result = await router.execute_tool("archive_task", {"task_id": "T-1"}, "s1")
    assert result.get("action") == "archive"
    assert seeded.get(Task, "T-1").archived_at is not None

    result = await router.execute_tool(
        "archive_task", {"task_id": "T-1", "restore": True}, "s1"
    )
    assert result.get("action") == "restore"
    assert seeded.get(Task, "T-1").archived_at is None


@pytest.mark.asyncio
async def test_suggest_agents_advisory(seeded):
    seeded.add(
        Agent(
            id="a-exec",
            name="Executor",
            role="executor",
            status="active",
            agent_type="cli",
            cli="claude",
            capabilities=["execute"],
        )
    )
    seeded.commit()
    router = CommandRouter(seeded)
    result = await router.execute_tool("suggest_agents", {"task_id": "T-1"}, "s1")
    assert result["task_id"] == "T-1"
    assert isinstance(result["suggestions"], list)
    # Advisory only: no run or gate was created.
    assert seeded.query(AgentRun).count() == 0


@pytest.mark.asyncio
async def test_query_db_agent_runs_and_audit(seeded):
    seeded.add(
        AgentRun(
            id="r-1",
            task_id="T-1",
            agent_id="a-exec",
            cli="claude",
            command="claude -p hi",
            status="success",
        )
    )
    seeded.add(AuditLog(task_id="T-1", action="gate_approved", actor="tester"))
    seeded.commit()

    router = CommandRouter(seeded)
    result = await router.execute_tool(
        "query_db", {"entity": "agent_runs", "filters": {"task_id": "T-1"}}, "s1"
    )
    assert result["count"] == 1
    assert result["rows"][0]["id"] == "r-1"
    assert result["rows"][0]["status"] == "success"

    result = await router.execute_tool(
        "query_db", {"entity": "audit", "filters": {"task_id": "T-1"}}, "s1"
    )
    assert result["count"] == 1
    assert result["rows"][0]["action"] == "gate_approved"


@pytest.mark.asyncio
async def test_query_db_knowledge_content_on_point_lookup(seeded):
    seeded.add(
        KnowledgeItem(id="k-1", title="Guide", category="howto", content="full body")
    )
    seeded.commit()
    router = CommandRouter(seeded)

    listing = await router.execute_tool("query_db", {"entity": "knowledge"}, "s1")
    assert "content" not in listing["rows"][0]

    point = await router.execute_tool(
        "query_db", {"entity": "knowledge", "filters": {"id": "k-1"}}, "s1"
    )
    assert point["rows"][0]["content"] == "full body"


@pytest.mark.asyncio
async def test_update_task_dependency_edits(seeded):
    router = CommandRouter(seeded)
    result = await router.execute_tool(
        "update_task",
        {"task_id": "T-2", "patch": {"add_depends_on": ["T-1"]}},
        "s1",
    )
    assert result["depends_on"] == ["T-1"]

    # Cycle is rejected.
    result = await router.execute_tool(
        "update_task",
        {"task_id": "T-1", "patch": {"add_depends_on": ["T-2"]}},
        "s1",
    )
    assert "error" in result

    result = await router.execute_tool(
        "update_task",
        {"task_id": "T-2", "patch": {"remove_depends_on": ["T-1"]}},
        "s1",
    )
    assert result["depends_on"] == []
    assert seeded.query(TaskDependency).count() == 0


@pytest.mark.asyncio
async def test_manage_agent_api_key_encrypted_never_plaintext(seeded, monkeypatch):
    monkeypatch.setenv("CT_CRYPTO_KEY", "")
    router = CommandRouter(seeded)
    result = await router.execute_tool(
        "manage_agent",
        {
            "action": "create",
            "id": "a-api",
            "name": "API agent",
            "role": "executor",
            "agent_type": "api",
            "provider": "openai",
            "api_key": "sk-super-secret",
            "mode": "bypass",
        },
        "s1",
    )
    assert "error" not in result, result

    agent = seeded.get(Agent, "a-api")
    assert agent is not None
    assert agent.api_key and agent.api_key != "sk-super-secret"
    assert decrypt_api_key(agent.api_key) == "sk-super-secret"

    # The plaintext key must not appear in the response, the admin-gate
    # ledger, or the audit trail.
    assert "sk-super-secret" not in json.dumps(result)
    from app.db.models import AdminGateRecord

    for record in seeded.query(AdminGateRecord).all():
        assert "sk-super-secret" not in json.dumps(record.input_payload or {})
        assert "sk-super-secret" not in json.dumps(record.output_payload or {})
