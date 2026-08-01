"""Telemetry for token-saving tools (CTV2-239)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import ToolMetric
from app.services import graph_client
from app.services.tool_metrics import record_tool_metric


@pytest.fixture
def metric_session(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("app.db.base.SessionLocal", factory)
    return factory


def test_record_tool_metric_persists_a_row(metric_session):
    record_tool_metric(
        tool="semantic_search", source="graph_client", ok=True,
        duration_ms=12, result_count=3, bytes_out=140,
    )
    db = metric_session()
    rows = db.query(ToolMetric).all()
    assert len(rows) == 1
    assert rows[0].tool == "semantic_search"
    assert rows[0].ok is True
    assert rows[0].result_count == 3


def test_record_tool_metric_never_raises(monkeypatch):
    monkeypatch.setattr("app.db.base.SessionLocal", MagicMock(side_effect=RuntimeError("db down")))
    record_tool_metric(tool="x", source="y", ok=False)  # must not raise


class _FakeMCP:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def call_tool(self, *a, **k):
        if self._error:
            raise self._error
        return self._result


@pytest.mark.asyncio
async def test_graph_success_records_ok_metric(monkeypatch):
    graph_client.clear_graph_cache()
    captured = []
    monkeypatch.setattr(graph_client, "record_tool_metric", lambda **kw: captured.append(kw))
    monkeypatch.setattr(
        graph_client, "MCPClient", lambda **kw: _FakeMCP(result={"results": [{"name": "a.py"}]})
    )

    out = await graph_client.semantic_search("/tmp/x", "query", use_cache=False)

    assert out == [{"name": "a.py"}]
    assert len(captured) == 1
    assert captured[0]["ok"] is True and captured[0]["tool"] == "semantic_search"
    assert captured[0]["result_count"] == 1


@pytest.mark.asyncio
async def test_graph_failure_records_error_metric(monkeypatch):
    graph_client.clear_graph_cache()
    captured = []
    monkeypatch.setattr(graph_client, "record_tool_metric", lambda **kw: captured.append(kw))
    monkeypatch.setattr(
        graph_client, "MCPClient", lambda **kw: _FakeMCP(error=RuntimeError("graph not built"))
    )

    out = await graph_client.semantic_search("/tmp/x", "query", use_cache=False)

    assert out == []  # silent fallback preserved
    assert len(captured) == 1
    assert captured[0]["ok"] is False
    assert "graph not built" in captured[0]["error"]


@pytest.mark.asyncio
async def test_graph_cache_hit_records_cached_metric(monkeypatch):
    graph_client.clear_graph_cache()
    captured = []
    monkeypatch.setattr(graph_client, "record_tool_metric", lambda **kw: captured.append(kw))
    monkeypatch.setattr(
        graph_client, "MCPClient", lambda **kw: _FakeMCP(result={"results": [{"name": "a.py"}]})
    )

    await graph_client.semantic_search("/tmp/x", "q2")
    await graph_client.semantic_search("/tmp/x", "q2")

    assert [c.get("cache_hit", False) for c in captured] == [False, True]
