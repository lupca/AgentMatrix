import logging
import time
from typing import Any, Dict, List, Optional, Union
from app.services.mcp import MCPClient

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300.0  # 5 minutes in seconds
DEFAULT_TIMEOUT = 30.0


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


def _make_cache_key(func_name: str, repo_root: str, **kwargs) -> str:
    sorted_args = sorted(kwargs.items())
    return f"{func_name}:{repo_root}:{sorted_args}"


async def get_impact_radius(
    repo_root: str,
    file: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> List[str]:
    """Get files affected by changes to given file."""
    cache_key = _make_cache_key("get_impact_radius", repo_root, file=file)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "get_impact_radius_tool",
                arguments={"repo_root": repo_root, "changed_files": [file]},
                timeout=timeout,
            )

        result: List[str] = []
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
        elif isinstance(raw, list):
            result = [str(item) for item in raw]

        if use_cache:
            graph_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("get_impact_radius failed with fallback to []: %s", e)
        return []


async def semantic_search(
    repo_root: str,
    query: str,
    limit: int = 10,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """Search nodes by semantic similarity."""
    cache_key = _make_cache_key("semantic_search", repo_root, query=query, limit=limit)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        async with MCPClient(repo_root=repo_root) as client:
            raw = await client.call_tool(
                "semantic_search_nodes_tool",
                arguments={"repo_root": repo_root, "query": query, "limit": limit},
                timeout=timeout,
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

        if use_cache:
            graph_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.warning("semantic_search failed with fallback to []: %s", e)
        return []


async def query_tests_for(
    repo_root: str,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> List[str]:
    """Find test files covering a target file/function."""
    cache_key = _make_cache_key("query_tests_for", repo_root, target=target)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            return cached

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
        return result
    except Exception as e:
        logger.warning("query_tests_for failed with fallback to []: %s", e)
        return []


async def get_affected_flows(
    repo_root: str,
    files: List[str],
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> List[str]:
    """Get business flows affected by file changes."""
    cache_key = _make_cache_key("get_affected_flows", repo_root, files=tuple(files))
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
            return cached

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
        return result
    except Exception as e:
        logger.warning("get_affected_flows failed with fallback to []: %s", e)
        return []


async def query_graph(
    repo_root: str,
    pattern: str,
    target: str,
    timeout: float = DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> Union[List[Dict[str, Any]], List[str]]:
    """Generic query_graph wrapper for predefined patterns."""
    cache_key = _make_cache_key("query_graph", repo_root, pattern=pattern, target=target)
    if use_cache:
        cached = graph_cache.get(cache_key)
        if cached is not None:
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
        return result
    except Exception as e:
        logger.warning("query_graph failed with fallback to []: %s", e)
        return []
