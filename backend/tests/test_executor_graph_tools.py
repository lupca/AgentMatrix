"""CTV2-1358: Executor gọi graph tools → tool_metrics có task_id khác NULL.

Kiểm chứng đường đi:
  MCP call (executor token) → mcp_native handler → CommandRouter →
  context_handlers._handle_get_impact_radius / _handle_get_minimal_context →
  graph_client.get_impact_radius / semantic_search →
  _observe → record_tool_metric(task_id=...)

Acceptance:
  - tool_metrics có dòng get_impact_radius hoặc semantic_search với ok=true
    VÀ task_id khác NULL
  - Executor vẫn bị giới hạn đúng task của nó
"""

from __future__ import annotations

import json

import pytest
from fastmcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.db.base as db_base
import app.mcp_native as mcp_native
from app.db.base import Base
from app.db.models import Project, Task, ToolMetric
from app.mcp_native import build_server, issue_token
from app.services import graph_client


class _FakeGraphMCP:
    """Mock MCPClient cho graph_client — trả về kết quả thành công."""

    def __init__(self, tool_responses: dict | None = None):
        self._responses = tool_responses or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, tool_name, arguments=None, **kw):
        if tool_name in self._responses:
            return self._responses[tool_name]
        if tool_name == "get_impact_radius_tool":
            return {
                "status": "ok",
                "impacted_files": ["backend/app/models.py", "backend/app/services/graph_client.py"],
                "risk": "medium",
                "impacted_file_count": 2,
            }
        if tool_name == "semantic_search_nodes_tool":
            return {
                "results": [
                    {"name": "get_impact_radius", "file_path": "backend/app/services/graph_client.py"},
                    {"name": "record_tool_metric", "file_path": "backend/app/services/tool_metrics.py"},
                ]
            }
        return {"results": []}


@pytest.fixture
def executor_db(monkeypatch):
    """SQLite scratch DB chứa Project + Task, monkeypatched vào SessionLocal."""
    monkeypatch.delenv("TESTING", raising=False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    seed = factory()
    seed.add(Project(id="proj-graph", name="Graph Project", repo_root="/tmp/fake-repo"))
    seed.add(Task(
        id="task-graph-1", title="Verify graph tools", project="proj-graph",
        status="dispatched",
    ))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", factory)
    monkeypatch.setattr(db_base, "SessionLocal", factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    yield factory

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.mark.asyncio
async def test_executor_get_impact_radius_records_metric_with_task_id(executor_db, monkeypatch):
    """Executor gọi get_impact_radius → tool_metrics có ok=true, task_id != NULL."""
    captured_metrics = []

    def _capture_metric(**kw):
        captured_metrics.append(kw)

    monkeypatch.setattr(graph_client, "record_tool_metric", _capture_metric)
    monkeypatch.setattr(
        graph_client, "MCPClient",
        lambda **kw: _FakeGraphMCP(),
    )
    graph_client.clear_graph_cache()

    token = issue_token("test-secret", role="executor", task_id="task-graph-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "get_impact_radius", {"file": "backend/app/services/graph_client.py"}
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    data = body["data"]
    assert data["status"] == "success"
    assert data["repo_root"] == "/tmp/fake-repo"

    graph_metrics = [m for m in captured_metrics if m["tool"] == "get_impact_radius"]
    assert len(graph_metrics) >= 1, (
        f"Expected at least one get_impact_radius metric, got: {captured_metrics}"
    )
    success_metrics = [m for m in graph_metrics if m["ok"] is True]
    assert len(success_metrics) >= 1
    for m in success_metrics:
        assert m["task_id"] == "task-graph-1", (
            f"tool_metrics task_id must be executor's task, got: {m['task_id']}"
        )


@pytest.mark.asyncio
async def test_executor_get_minimal_context_records_metric_with_task_id(executor_db, monkeypatch):
    """Executor gọi get_minimal_context → tool_metrics có ok=true, task_id != NULL."""
    captured_metrics = []

    def _capture_metric(**kw):
        captured_metrics.append(kw)

    monkeypatch.setattr(graph_client, "record_tool_metric", _capture_metric)
    monkeypatch.setattr(
        graph_client, "MCPClient",
        lambda **kw: _FakeGraphMCP(),
    )
    graph_client.clear_graph_cache()

    token = issue_token("test-secret", role="executor", task_id="task-graph-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "get_minimal_context", {"query": "graph_client", "limit": 5}
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    data = body["data"]
    assert data["status"] == "success"
    assert data["repo_root"] == "/tmp/fake-repo"

    search_metrics = [m for m in captured_metrics if m["tool"] == "semantic_search"]
    assert len(search_metrics) >= 1, (
        f"Expected at least one semantic_search metric, got: {captured_metrics}"
    )
    success_metrics = [m for m in search_metrics if m["ok"] is True]
    assert len(success_metrics) >= 1
    for m in success_metrics:
        assert m["task_id"] == "task-graph-1", (
            f"tool_metrics task_id must be executor's task, got: {m['task_id']}"
        )


@pytest.mark.asyncio
async def test_executor_graph_tool_persists_metric_to_db(executor_db, monkeypatch):
    """End-to-end: metric thực sự ghi vào DB tool_metrics với task_id."""
    monkeypatch.setattr(
        graph_client, "MCPClient",
        lambda **kw: _FakeGraphMCP(),
    )
    graph_client.clear_graph_cache()

    token = issue_token("test-secret", role="executor", task_id="task-graph-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "get_impact_radius", {"file": "backend/app/models.py"}
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body

    db = executor_db()
    rows = db.query(ToolMetric).filter(
        ToolMetric.tool == "get_impact_radius",
        ToolMetric.task_id.isnot(None),
    ).all()
    assert len(rows) >= 1, (
        "tool_metrics must have get_impact_radius row with non-NULL task_id"
    )
    for row in rows:
        assert row.ok is True
        assert row.task_id == "task-graph-1"
        assert row.source == "graph_client"
    db.close()
