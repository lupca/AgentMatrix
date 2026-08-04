import logging
import time
from typing import Any, Dict, List, Optional, Union
from app.services.mcp import MCPClient, MCPClientError, MCPToolError
from app.core.compression import compress_for_prompt
from app.services.tool_metrics import record_tool_metric

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300.0  # 5 minutes in seconds
DEFAULT_TIMEOUT = 30.0


class GraphClientError(RuntimeError):
    """Raised when a graph query could not be completed."""

    def __init__(self, message: str, *, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


def _is_graph_not_built_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "graph not built",
        "graph is not built",
        "graph has not been built",
        "no graph found",
        "graph database not found",
        "graph does not exist",
        "build the graph",
    )
    return any(marker in message for marker in markers)


def _graph_client_error(exc: Exception) -> GraphClientError:
    if isinstance(exc, GraphClientError):
        return exc
    if isinstance(exc, MCPToolError):
        if _is_graph_not_built_error(exc):
            return GraphClientError(
                f"Code graph has not been built: {exc}", kind="graph_not_built"
            )
        return GraphClientError(f"Graph MCP tool error: {exc}", kind="tool_error")
    if isinstance(exc, MCPClientError):
        return GraphClientError(f"Graph MCP transport error: {exc}", kind="transport")
    return GraphClientError(f"Graph MCP transport error: {exc}", kind="transport")


class TTLCache:
    """In-memory TTL cache for graph queries."""

    def __init__(self, default_ttl: float = DEFAULT_TTL):
        self.default_ttl = default_ttl
        self._cache: Dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            expires_at, val = self._cache[key]
            if time.monotonic() < expires_at:
                return val
            else:
                del self._cache[key]
        return None

    def set(self, key: str, val: Any, ttl: Optional[float] = None) -> None:
        duration = ttl if ttl is not None else self.default_ttl
        expires_at = time.monotonic() + duration
        self._cache[key] = (expires_at, val)

    def clear(self) -> None:
        self._cache.clear()


# Global cache instance
graph_cache = TTLCache()


def clear_graph_cache() -> None:
    """Clear all cached query results."""
    graph_cache.clear()


def _observe(
    tool: str,
    started: float,
    ok: bool,
    result=None,
    cached=False,
    error=None,
    task_id: str | None = None,
) -> None:
    """One tool_metrics row per graph call — success, failure, or cache hit."""
    record_tool_metric(
        tool=tool,
        source="graph_client",
        ok=ok,
        cache_hit=cached,
        duration_ms=int((time.monotonic() - started) * 1000),
        result_count=(len(result) if isinstance(result, (list, dict)) else None),
        bytes_out=(len(str(result)) if result is not None else None),
        error=error,
        task_id=task_id,
    )


def _make_cache_key(func_name: str, repo_root: str, **kwargs) -> str:
    sorted_args = sorted(kwargs.items())
    return f"{func_name}:{repo_root}:{sorted_args}"


async def check_graph_staleness(
    repo_root: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """Check if the code graph at repo_root is stale relative to git HEAD.

    Returns dict with keys:
      - is_stale: bool
      - built_at_sha: str | None
      - head_sha: str | None
      - warning: str | None
    """
    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "list_graph_stats_tool",
                arguments={"repo_root": repo_root},
                timeout=timeout,
            )
        if isinstance(raw, dict):
            graph_meta = raw.get("_graph") or {}
            built_at_sha = graph_meta.get("built_at_sha") or raw.get("built_at_commit")
            head_sha = graph_meta.get("head_sha") or raw.get("current_sha")
            head_matches_build = graph_meta.get("head_matches_build")
            if head_matches_build is None and built_at_sha and head_sha:
                head_matches_build = (built_at_sha == head_sha)
            if head_matches_build is False:
                sha_str = built_at_sha or "unknown"
                return {
                    "is_stale": True,
                    "built_at_sha": built_at_sha,
                    "head_sha": head_sha,
                    "warning": f"graph đang cũ tại {sha_str}",
                }
            return {
                "is_stale": False,
                "built_at_sha": built_at_sha,
                "head_sha": head_sha,
                "warning": None,
            }
    except Exception as exc:
        logger.warning("check_graph_staleness check failed: %s", exc)

    return {
        "is_stale": False,
        "built_at_sha": None,
        "head_sha": None,
        "warning": None,
    }


async def rebuild_graph_incremental(
    repo_root: str,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """Run incremental build and vector embedding on code-review-graph for repo_root."""
    async with MCPClient(repo_root=repo_root) as client:
        build_res = await client.call_tool(
            "build_or_update_graph_tool",
            arguments={"repo_root": repo_root, "full_rebuild": False},
            timeout=timeout,
        )
        embed_res = await client.call_tool(
            "embed_graph_tool",
            arguments={"repo_root": repo_root},
            timeout=timeout,
        )
    clear_graph_cache()
    return {"build": build_res, "embed": embed_res}


async def get_impact_radius(
    repo_root: str,
    file: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    compress_output: bool = False,
    raise_on_error: bool = False,
    detail_level: str = "minimal",
    max_depth: int = 2,
    task_id: str | None = None,
) -> Union[List[str], Dict[str, Any], str]:
    """Get files affected by changes to given file.

    Args:
        compress_output: If True, return compressed string for prompt usage.
        detail_level: Graph response verbosity. Defaults to ``minimal`` so hub
            files do not produce unnecessarily large MCP payloads.
        max_depth: Maximum graph traversal depth.
    """
    started = time.monotonic()
    _tool_name = "get_impact_radius"
    cache_key = _make_cache_key(
        "get_impact_radius",
        repo_root,
        file=file,
        detail_level=detail_level,
        max_depth=max_depth,
    )
    if use_cache and not raise_on_error:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            _observe(_tool_name, started, True, cached, cached=True, task_id=task_id)
            return compress_for_prompt(cached) if compress_output else cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "get_impact_radius_tool",
                arguments={
                    "repo_root": repo_root,
                    "changed_files": [file],
                    "detail_level": detail_level,
                    "max_depth": max_depth,
                },
                timeout=timeout,
                raise_on_error=raise_on_error,
            )

        result: Union[List[str], Dict[str, Any]] = []
        if isinstance(raw, dict):
            if "impacted_files" in raw and isinstance(raw["impacted_files"], list):
                result = [str(f) for f in raw["impacted_files"]]
            elif "impacted_nodes" in raw and isinstance(raw["impacted_nodes"], list):
                result = [
                    str(n.get("file_path") or n.get("name") or n)
                    for n in raw["impacted_nodes"]
                    if isinstance(n, dict)
                ]
            elif "nodes" in raw and isinstance(raw["nodes"], list):
                result = [
                    str(n.get("file_path") or n.get("name") or n)
                    for n in raw["nodes"]
                    if isinstance(n, dict)
                ]
            elif raw.get("status") == "ok" and any(
                key in raw
                for key in ("summary", "risk", "impacted_file_count", "key_entities")
            ):
                # detail_level="minimal" intentionally omits the potentially huge
                # impacted_files/impacted_nodes arrays. Preserve its useful summary
                # instead of misreporting a successful query as no impact.
                result = raw
        elif isinstance(raw, list):
            result = [str(item) for item in raw]

        if use_cache and raw is not None:
            graph_cache.set(cache_key, result)
        if raise_on_error and raw is None:
            raise GraphClientError(
                "Graph MCP returned an empty response", kind="empty_response"
            )
        _observe(_tool_name, started, True, result, task_id=task_id)
        return compress_for_prompt(result) if compress_output else result
    except Exception as e:
        logger.warning("get_impact_radius failed with fallback to []: %s", e)
        _observe("get_impact_radius", started, False, error=str(e), task_id=task_id)
        if raise_on_error:
            error = _graph_client_error(e)
            raise error from e
        return compress_for_prompt([]) if compress_output else []


async def semantic_search(
    repo_root: str,
    query: str,
    limit: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    compress_output: bool = False,
    raise_on_error: bool = False,
    task_id: str | None = None,
) -> Union[List[Dict[str, Any]], str]:
    """Search nodes by semantic similarity.

    Args:
        compress_output: If True, return compressed string for prompt usage.
    """
    started = time.monotonic()
    _tool_name = "semantic_search"
    cache_key = _make_cache_key("semantic_search", repo_root, query=query, limit=limit)
    if use_cache and not raise_on_error:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            _observe(_tool_name, started, True, cached, cached=True, task_id=task_id)
            return compress_for_prompt(cached) if compress_output else cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "semantic_search_nodes_tool",
                arguments={"repo_root": repo_root, "query": query, "limit": limit},
                timeout=timeout,
                raise_on_error=raise_on_error,
            )

        result: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            result = [item for item in raw if isinstance(item, dict)][:limit]
        elif isinstance(raw, dict):
            if "results" in raw and isinstance(raw["results"], list):
                result = [item for item in raw["results"] if isinstance(item, dict)][:limit]
            elif "nodes" in raw and isinstance(raw["nodes"], list):
                result = [item for item in raw["nodes"] if isinstance(item, dict)][:limit]
            else:
                result = [raw]

        if use_cache and raw is not None:
            graph_cache.set(cache_key, result)
        if raise_on_error and raw is None:
            raise GraphClientError(
                "Graph MCP returned an empty response", kind="empty_response"
            )
        _observe(_tool_name, started, True, result, task_id=task_id)
        return compress_for_prompt(result) if compress_output else result
    except Exception as e:
        logger.warning("semantic_search failed with fallback to []: %s", e)
        _observe("semantic_search", started, False, error=str(e), task_id=task_id)
        if raise_on_error:
            error = _graph_client_error(e)
            raise error from e
        return compress_for_prompt([]) if compress_output else []


async def query_tests_for(
    repo_root: str,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    compress_output: bool = False,
) -> Union[List[str], str]:
    """Find test files covering a target file/function.

    Args:
        compress_output: If True, return compressed string for prompt usage.
    """
    started = time.monotonic()
    _tool_name = "query_tests_for"
    cache_key = _make_cache_key("query_tests_for", repo_root, target=target)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            _observe(_tool_name, started, True, cached, cached=True)
            return compress_for_prompt(cached) if compress_output else cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "query_graph_tool",
                arguments={"repo_root": repo_root, "pattern": "tests_for", "target": target},
                timeout=timeout,
            )

        result: List[str] = []
        if isinstance(raw, dict) and "results" in raw and isinstance(raw["results"], list):
            for item in raw["results"]:
                if isinstance(item, dict):
                    val = item.get("file_path") or item.get("name") or str(item)
                    result.append(str(val))
                elif isinstance(item, str):
                    result.append(item)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    val = item.get("file_path") or item.get("name") or str(item)
                    result.append(str(val))
                elif isinstance(item, str):
                    result.append(item)

        if use_cache:
            graph_cache.set(cache_key, result)
        _observe(_tool_name, started, True, result)
        return compress_for_prompt(result) if compress_output else result
    except Exception as e:
        logger.warning("query_tests_for failed with fallback to []: %s", e)
        _observe("query_tests_for", started, False, error=str(e))
        return compress_for_prompt([]) if compress_output else []


async def get_affected_flows(
    repo_root: str,
    files: List[str],
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
    compress_output: bool = False,
) -> Union[List[str], str]:
    """Get business flows affected by file changes.

    Args:
        compress_output: If True, return compressed string for prompt usage.
    """
    started = time.monotonic()
    _tool_name = "get_affected_flows"
    cache_key = _make_cache_key("get_affected_flows", repo_root, files=tuple(files))
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            _observe(_tool_name, started, True, cached, cached=True)
            return compress_for_prompt(cached) if compress_output else cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "get_affected_flows_tool",
                arguments={"repo_root": repo_root, "changed_files": files},
                timeout=timeout,
            )

        result: List[str] = []
        if isinstance(raw, dict):
            if "affected_flows" in raw and isinstance(raw["affected_flows"], list):
                for flow in raw["affected_flows"]:
                    if isinstance(flow, dict):
                        result.append(str(flow.get("name") or flow.get("id") or flow))
                    else:
                        result.append(str(flow))
            elif "flows" in raw and isinstance(raw["flows"], list):
                for flow in raw["flows"]:
                    if isinstance(flow, dict):
                        result.append(str(flow.get("name") or flow.get("id") or flow))
                    else:
                        result.append(str(flow))
        elif isinstance(raw, list):
            result = [str(f) for f in raw]

        if use_cache:
            graph_cache.set(cache_key, result)
        _observe(_tool_name, started, True, result)
        return compress_for_prompt(result) if compress_output else result
    except Exception as e:
        logger.warning("get_affected_flows failed with fallback to []: %s", e)
        _observe("get_affected_flows", started, False, error=str(e))
        return compress_for_prompt([]) if compress_output else []


async def query_graph(
    repo_root: str,
    pattern: str,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> Union[List[Dict[str, Any]], List[str]]:
    """Generic query_graph wrapper for predefined patterns."""
    started = time.monotonic()
    _tool_name = "query_graph"
    cache_key = _make_cache_key("query_graph", repo_root, pattern=pattern, target=target)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            _observe(_tool_name, started, True, cached, cached=True)
            return cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "query_graph_tool",
                arguments={"repo_root": repo_root, "pattern": pattern, "target": target},
                timeout=timeout,
            )

        result: Union[List[Dict[str, Any]], List[str]] = []
        if isinstance(raw, dict) and "results" in raw and isinstance(raw["results"], list):
            result = raw["results"]
        elif isinstance(raw, list):
            result = raw

        if use_cache:
            graph_cache.set(cache_key, result)
        _observe(_tool_name, started, True, result)
        return result
    except Exception as e:
        logger.warning("query_graph failed with fallback to []: %s", e)
        _observe("query_graph", started, False, error=str(e))
        return []


async def symbol_exists(
    repo_root: str,
    symbol: str,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Check symbol resolvability using the graph's node resolver.

    The graph server has no standalone symbol-existence pattern.  The
    ``callers_of`` query resolves its target before looking for edges, so its
    ``ok``/``not_found`` status answers existence even when there are no callers.
    """

    started = time.monotonic()
    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "query_graph_tool",
                arguments={
                    "repo_root": repo_root,
                    "pattern": "callers_of",
                    "target": symbol,
                    "detail_level": "minimal",
                    "max_results": 1,
                },
                timeout=timeout,
            )
        exists = isinstance(raw, dict) and raw.get("status") == "ok"
        _observe("symbol_exists", started, True, {"exists": exists})
        return exists
    except Exception as exc:
        logger.warning("symbol_exists failed with fallback to false: %s", exc)
        _observe("symbol_exists", started, False, error=str(exc))
        return False
