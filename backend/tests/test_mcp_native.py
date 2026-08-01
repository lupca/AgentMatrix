from __future__ import annotations

import json

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.mcp_native as mcp_native
from app.db.base import Base
from app.db.models import Project, Task
from app.mcp_native import authenticate_token, build_server, envelope, issue_token


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
