"""Tests for the FastMCP projection (ADR-001 §D5, CTV2-084).

Covers: the registry -> MCP tool projection stays schema-faithful, the
``POST /api/mcp/tools/call`` endpoint enforces the scoped token, and an MCP
tool call produces the same DB result as the equivalent API-mode
``CommandRouter.execute_tool`` call (parity).
"""

from __future__ import annotations

import httpx
import pytest

import app.mcp_server as mcp_server_module
from app.core.config import settings
from app.db.base import get_db
from app.db.models import Project, Task
from app.main import app
from app.services.command_router import CommandRouter
from app.services.tool_registry import TOOL_REGISTRY, get_mcp_tool_specs


TEST_TOKEN = "test-scoped-mcp-token"  # noqa: S105 - test fixture, not a real secret


@pytest.fixture(autouse=True)
def mcp_token(monkeypatch):
    """Configure the scoped token every test in this module relies on."""

    monkeypatch.setattr(settings, "MCP_API_TOKEN", TEST_TOKEN)
    yield TEST_TOKEN


# --- registry projection -----------------------------------------------


def test_get_mcp_tool_specs_excludes_only_the_openai_meta_tool():
    specs = get_mcp_tool_specs()
    names = {spec.name for spec in specs}

    assert "load_tools" not in names
    assert names == set(TOOL_REGISTRY) - {"load_tools"}
    assert len(specs) == len(TOOL_REGISTRY) - 1


@pytest.mark.asyncio
async def test_build_server_registers_every_tool_with_registry_schema():
    server = mcp_server_module.build_server(
        api_url="http://testserver", token=TEST_TOKEN, session_id="mcp-cli"
    )
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {spec.name for spec in get_mcp_tool_specs()}
    for name, tool in tools.items():
        spec = TOOL_REGISTRY[name]
        assert tool.description == spec.description
        assert tool.parameters == spec.parameters


# --- REST endpoint auth --------------------------------------------------


def test_mcp_tool_call_rejects_missing_token(client):
    response = client.post(
        "/api/mcp/tools/call",
        json={"tool": "get_status", "arguments": {}, "session_id": "s"},
    )
    assert response.status_code == 401


def test_mcp_tool_call_rejects_wrong_token(client):
    response = client.post(
        "/api/mcp/tools/call",
        json={"tool": "get_status", "arguments": {}, "session_id": "s"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_mcp_tool_call_fails_closed_when_no_token_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "MCP_API_TOKEN", "")
    response = client.post(
        "/api/mcp/tools/call",
        json={"tool": "get_status", "arguments": {}, "session_id": "s"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 401


def test_mcp_tool_call_accepts_valid_token(client):
    response = client.post(
        "/api/mcp/tools/call",
        json={"tool": "get_status", "arguments": {}, "session_id": "s"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_mcp_tool_call_reports_unknown_tool(client):
    response = client.post(
        "/api/mcp/tools/call",
        json={"tool": "not_a_real_tool", "arguments": {}, "session_id": "s"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 200
    assert "error" in response.json()


# --- MCP <-> API mode parity ----------------------------------------------


@pytest.fixture
def asgi_mcp_server(db_session, monkeypatch):
    """Route the MCP server's outbound HTTP calls into the in-process app.

    Swaps ``httpx.AsyncClient`` inside ``mcp_server`` for one wired to an
    ``ASGITransport`` bound to the same FastAPI ``app`` + ``db_session`` the
    rest of the test uses, so an MCP tool call exercises the real endpoint
    (auth, ``CommandRouter.execute_tool``, commit) without a subprocess or a
    real network hop.
    """

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    real_async_client = httpx.AsyncClient

    def fake_async_client(*, base_url, timeout):
        return real_async_client(
            transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=timeout
        )

    monkeypatch.setattr(mcp_server_module.httpx, "AsyncClient", fake_async_client)

    server = mcp_server_module.build_server(
        api_url="http://testserver", token=TEST_TOKEN, session_id="mcp-cli"
    )
    yield server
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mcp_create_task_matches_api_mode_execute_tool(db_session, asgi_mcp_server):
    db_session.add(Project(id="parity", name="Parity"))
    db_session.commit()

    api_result = await CommandRouter(db_session).execute_tool(
        "create_task",
        {"title": "Via API mode", "project": "parity"},
        "api-session",
    )
    assert api_result["action"] == "created"

    mcp_result = await asgi_mcp_server.call_tool(
        "create_task", {"title": "Via MCP", "project": "parity"}
    )
    mcp_payload = mcp_result.structured_content
    assert mcp_payload["action"] == "created"

    api_task = db_session.query(Task).filter(Task.id == api_result["task_id"]).one()
    mcp_task = db_session.query(Task).filter(Task.id == mcp_payload["task_id"]).one()

    assert api_task.project == mcp_task.project == "parity"
    assert api_task.status == mcp_task.status
    assert api_task.current_gate == mcp_task.current_gate
    assert {api_task.id, mcp_task.id} == {
        row.id for row in db_session.query(Task).filter(Task.project == "parity")
    }


@pytest.mark.asyncio
async def test_mcp_get_status_matches_api_mode_for_same_task(db_session, asgi_mcp_server):
    db_session.add(Project(id="parity", name="Parity"))
    db_session.commit()

    created = await CommandRouter(db_session).execute_tool(
        "create_task", {"title": "Lookup me", "project": "parity"}, "api-session"
    )
    task_id = created["task_id"]

    api_result = await CommandRouter(db_session).execute_tool(
        "get_status", {"task_id": task_id}, "api-session"
    )
    mcp_result = await asgi_mcp_server.call_tool("get_status", {"task_id": task_id})

    assert api_result["task"] == mcp_result.structured_content["task"]


@pytest.mark.asyncio
async def test_mcp_tool_call_cannot_bypass_four_eyes_gate(db_session, asgi_mcp_server):
    """Server-side gate enforcement holds regardless of the entry point."""

    db_session.add(Project(id="parity", name="Parity"))
    db_session.commit()

    created = await CommandRouter(db_session).execute_tool(
        "create_task", {"title": "Gate check", "project": "parity"}, "api-session"
    )
    task_id = created["task_id"]

    result = await asgi_mcp_server.call_tool(
        "record_verdict",
        {"task_id": task_id, "verdict": "pass"},
    )
    # Task was never dispatched, so the gate must reject the verdict from
    # the MCP path exactly like it would from API mode.
    assert "error" in result.structured_content
