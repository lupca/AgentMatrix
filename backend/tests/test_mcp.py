import asyncio
import os
import shutil
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.mcp import MCPClient, MCPClientError
from app.services.graph_client import (
    GraphClientError,
    TTLCache,
    clear_graph_cache,
    get_affected_flows,
    get_impact_radius,
    query_graph,
    query_tests_for,
    semantic_search,
)


@pytest.fixture(autouse=True)
def reset_cache():
    clear_graph_cache()
    yield
    clear_graph_cache()


# Unit Tests for TTLCache
def test_ttl_cache_basic():
    cache = TTLCache(default_ttl=1.0)
    cache.set("key1", "val1")
    assert cache.get("key1") == "val1"

    time.sleep(1.1)
    assert cache.get("key1") is None


def test_ttl_cache_clear():
    cache = TTLCache()
    cache.set("key1", "val1")
    cache.clear()
    assert cache.get("key1") is None


# Unit Tests for MCPClient
@pytest.mark.asyncio
async def test_mcp_client_binary_not_found():
    client = MCPClient(binary_path="/invalid/nonexistent/binary_path")
    with pytest.raises(MCPClientError, match="binary not found"):
        await client.connect()


@pytest.mark.asyncio
async def test_mcp_client_handshake_and_tool_call_mocked():
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.drain = AsyncMock()
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdout = MagicMock()

    # Sequence of responses:
    # 1. initialize response
    # 2. tools/call response
    init_res = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n'
    tool_res = b'{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\\"impacted_files\\": [\\"file1.py\\", \\"file2.py\\"]}"}]}}\n'

    mock_proc.stdout.readline = AsyncMock(side_effect=[init_res, tool_res])

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        async with MCPClient(binary_path="/usr/bin/python3") as client:
            res = await client.call_tool("get_impact_radius_tool", {"changed_files": ["main.py"]})
            assert isinstance(res, dict)
            assert res.get("impacted_files") == ["file1.py", "file2.py"]


@pytest.mark.asyncio
async def test_mcp_client_timeout_fallback():
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.drain = AsyncMock()
    mock_proc.stdout = MagicMock()

    # Handshake succeeds, but tool call times out
    init_res = b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05"}}\n'
    mock_proc.stdout.readline = AsyncMock(side_effect=[init_res, asyncio.TimeoutError()])

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        client = MCPClient(binary_path="/usr/bin/python3")
        res = await client.call_tool("slow_tool", timeout=0.1)
        assert res is None


# Unit Tests for High-level Graph Client Wrappers
@pytest.mark.asyncio
async def test_graph_client_get_impact_radius_fallback():
    with patch("app.services.graph_client.MCPClient.call_tool", side_effect=Exception("Connection error")):
        result = await get_impact_radius("/fake/repo", "src/index.ts", use_cache=False)
        assert result == []


@pytest.mark.asyncio
async def test_graph_client_semantic_search_fallback():
    with patch("app.services.graph_client.MCPClient.call_tool", side_effect=Exception("Timeout")):
        result = await semantic_search("/fake/repo", "search_term", use_cache=False)
        assert result == []


@pytest.mark.asyncio
async def test_graph_client_query_tests_for_fallback():
    with patch("app.services.graph_client.MCPClient.call_tool", return_value=None):
        result = await query_tests_for("/fake/repo", "src/util.py", use_cache=False)
        assert result == []


@pytest.mark.asyncio
async def test_graph_client_get_affected_flows_fallback():
    with patch("app.services.graph_client.MCPClient.call_tool", return_value="Invalid non-json"):
        result = await get_affected_flows("/fake/repo", ["src/util.py"], use_cache=False)
        assert result == []


@pytest.mark.asyncio
async def test_graph_client_caching():
    mock_response = {"impacted_files": ["a.py", "b.py"]}
    with patch("app.services.graph_client.MCPClient.call_tool", return_value=mock_response) as mock_call:
        # First call should hit MCP client
        res1 = await get_impact_radius("/fake/repo", "main.py", use_cache=True)
        assert res1 == ["a.py", "b.py"]
        assert mock_call.call_count == 1

        # Second call should return cached result without calling MCP client again
        res2 = await get_impact_radius("/fake/repo", "main.py", use_cache=True)
        assert res2 == ["a.py", "b.py"]
        assert mock_call.call_count == 1

        # Call with use_cache=False should query MCP client again
        res3 = await get_impact_radius("/fake/repo", "main.py", use_cache=False)
        assert res3 == ["a.py", "b.py"]
        assert mock_call.call_count == 2


# raise_on_error path (used by the coordinator's research tools so a
# graph-not-built / MCP-down condition surfaces as a structured error
# instead of silently looking like "no impact / no matches").
@pytest.mark.asyncio
async def test_graph_client_get_impact_radius_raises_when_no_response():
    with patch("app.services.graph_client.MCPClient.call_tool", return_value=None):
        with pytest.raises(GraphClientError, match="graph may not be built"):
            await get_impact_radius(
                "/fake/repo", "src/index.ts", use_cache=False, raise_on_error=True
            )


@pytest.mark.asyncio
async def test_graph_client_get_impact_radius_raises_on_connection_error():
    with patch(
        "app.services.graph_client.MCPClient.call_tool",
        side_effect=Exception("Connection error"),
    ):
        with pytest.raises(GraphClientError, match="Connection error"):
            await get_impact_radius(
                "/fake/repo", "src/index.ts", use_cache=False, raise_on_error=True
            )


@pytest.mark.asyncio
async def test_graph_client_semantic_search_raises_when_no_response():
    with patch("app.services.graph_client.MCPClient.call_tool", return_value=None):
        with pytest.raises(GraphClientError, match="graph may not be built"):
            await semantic_search(
                "/fake/repo", "search_term", use_cache=False, raise_on_error=True
            )


@pytest.mark.asyncio
async def test_graph_client_semantic_search_raises_on_connection_error():
    with patch(
        "app.services.graph_client.MCPClient.call_tool",
        side_effect=Exception("Timeout"),
    ):
        with pytest.raises(GraphClientError, match="Timeout"):
            await semantic_search(
                "/fake/repo", "search_term", use_cache=False, raise_on_error=True
            )


@pytest.mark.asyncio
async def test_graph_client_get_impact_radius_succeeds_with_raise_on_error_set():
    mock_response = {"impacted_files": ["a.py", "b.py"]}
    with patch("app.services.graph_client.MCPClient.call_tool", return_value=mock_response):
        result = await get_impact_radius(
            "/fake/repo", "main.py", use_cache=False, raise_on_error=True
        )
        assert result == ["a.py", "b.py"]


# Integration Test with real code-review-graph if installed
HAS_CRG = shutil.which("code-review-graph") is not None or os.path.exists("/home/lupca/.local/bin/code-review-graph")


@pytest.mark.skipif(not HAS_CRG, reason="code-review-graph binary not installed")
@pytest.mark.asyncio
async def test_real_code_review_graph_integration():
    repo_root = "/home/lupca/projects/control-tower"
    if not os.path.exists(repo_root):
        pytest.skip("Test repo not found")

    res = await get_impact_radius(repo_root=repo_root, file="scripts/ct-dispatch.py", use_cache=False)
    assert isinstance(res, list)

    flows = await get_affected_flows(repo_root=repo_root, files=["scripts/ct-dispatch.py"], use_cache=False)
    assert isinstance(flows, list)

    tests = await query_tests_for(repo_root=repo_root, target="scripts/ct-dispatch.py", use_cache=False)
    assert isinstance(tests, list)
