from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.mcp_native as mcp_native
from app.db.base import Base
from app.db.models import Agent, InboxItem, Project, Task
from app.mcp_native import authenticate_token, build_server, envelope, issue_token
from app.services.tool_registry import get_mcp_tool_specs
from app.services.task_orchestration import TaskOrchestrationService


def test_role_token_round_trip_and_task_scope_claim():
    token = issue_token("secret", role="executor", task_id="task-1")
    claims = authenticate_token(token, secret="secret")
    assert claims is not None
    assert claims.role == "executor"
    assert claims.task_id == "task-1"
    assert authenticate_token(token, secret="wrong") is None


def test_unsigned_or_legacy_token_is_rejected():
    assert authenticate_token("legacy", secret="secret") is None


def test_native_envelope_structures_transition_error_and_hint():
    result = envelope({"error": "Task is already dispatched; expected status todo"})
    assert result["ok"] is False
    assert result["error"]["code"] == "task_transition_conflict"
    assert "hint" in result["error"]


def test_native_envelope_includes_next_for_task_state():
    result = envelope(
        {"task": {"id": "task-1", "status": "awaiting-review"}},
        next_step="Gọi request_review để bắt đầu review độc lập.",
    )
    assert result["ok"] is True
    assert result["next"] == "Gọi request_review để bắt đầu review độc lập."


def test_pending_review_order_includes_the_persisted_approval_prompt(db_session):
    db_session.add(Project(id="pending-review-project", name="P", repo_root="/tmp"))
    db_session.add_all([
        Agent(id="@pending-executor", name="Executor", role="executor", cli="codex"),
        Agent(id="@pending-reviewer", name="Reviewer", role="reviewer", cli="codex"),
    ])
    db_session.add(
        Task(
            id="PENDING-REVIEW",
            title="Review reminder",
            project="pending-review-project",
            status="awaiting-review",
            mode="supervised",
            executor="@pending-executor",
            result_ref="base..head",
        )
    )
    db_session.commit()
    result = TaskOrchestrationService(db_session).request_review(
        task_id="PENDING-REVIEW",
        reviewer="@pending-reviewer",
        actor="system:test",
        idempotency_key="pending-review-gate",
        selection_reason="best capability match with 90% success_rate",
    )

    pending = mcp_native._pending_approvals(db_session)

    assert pending == [{
        "id": "PENDING-REVIEW",
        "kind": "task:review_order",
        "waiting_since": pending[0]["waiting_since"],
        "prompt": result.gate_record.input_payload["approval_prompt"],
    }]
    assert "Reviewer đề xuất: @pending-reviewer" in pending[0]["prompt"]
    assert "best capability match" in pending[0]["prompt"]


@pytest.mark.asyncio
async def test_tool_call_end_to_end_through_mcp_client(monkeypatch):
    """Full call path through a real fastmcp client. Regression for the
    handler signature bug: FunctionTool built from an explicit JSON schema
    never receives an injected Context, so a handler requiring one fails on
    every single call — which only surfaces when a tool is actually invoked
    through the protocol, not when the handler is unit-called directly."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="T-1", title="One", project="p1", status="todo"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="coordinator")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool("get_status", {"task_id": "T-1"})
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    assert body["data"]["task"]["id"] == "T-1"


@pytest.mark.asyncio
async def test_mcp_tool_list_is_filtered_by_token_role(monkeypatch):
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    coordinator = build_server(
        default_token=issue_token("test-secret", role="coordinator")
    )
    executor = build_server(
        default_token=issue_token("test-secret", role="executor", task_id="task-1")
    )

    async with Client(coordinator) as coordinator_client:
        coordinator_tools = await coordinator_client.list_tools()
    async with Client(executor) as executor_client:
        executor_tools = await executor_client.list_tools()

    # Suy số lượng từ registry, KHÔNG hard-code: thêm tool mới là con số đổi.
    # Bản đầu hard-code 28/7 và vỡ ngay khi CTV2-1341 thêm spec_write/spec_get
    # (28 -> 30) dù logic lọc vẫn đúng.
    all_specs = get_mcp_tool_specs()
    expected_executor = {
        spec.name for spec in all_specs if spec.required_role == "executor"
    }

    assert len(coordinator_tools) == len(all_specs)
    assert len(executor_tools) == len(expected_executor)
    assert {tool.name for tool in executor_tools} == expected_executor
    assert {tool.name for tool in executor_tools} <= {
        tool.name for tool in coordinator_tools
    }
    assert "manage_agent" in {tool.name for tool in coordinator_tools}
    assert "manage_agent" not in {tool.name for tool in executor_tools}

    async with Client(executor) as executor_client:
        forbidden = await executor_client.call_tool("manage_agent", {})
    assert json.loads(forbidden.content[0].text)["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_mcp_tool_list_filters_each_http_connection_by_its_token(monkeypatch):
    """One HTTP server must project a different tools/list for each token."""
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    app = mcp_native.build_http_app()

    def httpx_client_factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            **kwargs,
        )

    async with app.router.lifespan_context(app):
        tokens = [
            issue_token("test-secret", role="coordinator"),
            issue_token("test-secret", role="executor", task_id="task-1"),
        ]
        projections = []
        for token in tokens:
            transport = StreamableHttpTransport(
                "http://testserver/mcp",
                auth=token,
                httpx_client_factory=httpx_client_factory,
            )
            async with Client(transport) as client:
                projections.append(await client.list_tools())

    all_specs = get_mcp_tool_specs()
    executor_names = {
        spec.name for spec in all_specs if spec.required_role == "executor"
    }
    assert len(projections[0]) == len(all_specs)
    assert len(projections[1]) == len(executor_names)
    assert "manage_agent" in {tool.name for tool in projections[0]}
    assert "manage_agent" not in {tool.name for tool in projections[1]}


@pytest.mark.asyncio
async def test_tool_call_without_token_is_unauthorized(monkeypatch):
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server()  # no default token, no HTTP headers
    async with Client(server) as client:
        result = await client.call_tool("get_status", {"task_id": "T-1"})
        body = json.loads(result.content[0].text)
    assert body["ok"] is False
    assert body["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_save_project_context_executor_token_matching_task_id_succeeds(monkeypatch):
    """Regression for F1: the ToolSpec for save_project_context had no task_id
    parameter, so _task_scope_ok rejected every executor call with
    task_scope_violation before the handler ever ran."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "save_project_context",
            {
                "task_id": "task-1",
                "project_id": "p1",
                "context_md": "# Stack\nFastAPI",
                "rules": [],
            },
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    assert body["data"]["status"] == "success"
    assert body["data"]["project_id"] == "p1"


@pytest.mark.asyncio
async def test_save_project_context_executor_token_mismatched_task_id_rejected(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.add(Task(id="task-2", title="Two", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "save_project_context",
            {
                "task_id": "task-2",
                "project_id": "p1",
                "context_md": "# Stack\nFastAPI",
                "rules": [],
            },
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is False, body
    assert body["error"]["code"] == "task_scope_violation"


@pytest.mark.asyncio
async def test_manage_inbox_crud_mapping_and_promote_end_to_end(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="ideas", name="Ideas", repo_root="/tmp", task_prefix="IDEA"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))

    async with Client(server) as client:
        bad = await client.call_tool("manage_inbox", {"action": "add", "content": "x", "project_id": "missing"})
        assert "does not exist" in json.loads(bad.content[0].text)["error"]["message"]

        added = await client.call_tool("manage_inbox", {
            "action": "add", "content": "Build an inbox", "project_id": "ideas", "tags": ["product"]
        })
        add_body = json.loads(added.content[0].text)
        assert add_body["ok"] is True, add_body
        item_id = add_body["data"]["item"]["id"]

        updated = await client.call_tool("manage_inbox", {
            "action": "update", "id": item_id,
            "patch": {"content": "Build a better inbox", "tags": ["product", "v2"], "status": "open"},
        })
        assert json.loads(updated.content[0].text)["data"]["item"]["content"] == "Build a better inbox"

        listed = await client.call_tool("manage_inbox", {"action": "list", "q": "better", "status": "open"})
        assert json.loads(listed.content[0].text)["data"]["count"] == 1

        promoted = await client.call_tool("manage_inbox", {"action": "promote", "id": item_id, "title": "Inbox task"})
        promote_body = json.loads(promoted.content[0].text)
        assert promote_body["ok"] is True, promote_body
        assert promote_body["data"]["task_id"] == "IDEA-001"
        assert promote_body["data"]["item"]["status"] == "triaged"

        verify = session_factory()
        task = verify.get(Task, "IDEA-001")
        item = verify.get(InboxItem, item_id)
        assert task.raw_input == "Build a better inbox"
        assert item.task_id == task.id
        verify.close()

        second = await client.call_tool("manage_inbox", {"action": "add", "content": "Delete me"})
        second_id = json.loads(second.content[0].text)["data"]["item"]["id"]
        deleted = await client.call_tool("manage_inbox", {"action": "delete", "id": second_id})
        assert json.loads(deleted.content[0].text)["data"]["action"] == "deleted"
