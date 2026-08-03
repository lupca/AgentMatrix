import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_STDIO_BUFFER_LIMIT = 8 * 1024 * 1024


class MCPClientError(Exception):
    """Base exception for MCPClient errors."""
    pass


class MCPTransportError(MCPClientError):
    """Raised when the stdio transport cannot deliver a complete response."""


class MCPToolError(MCPClientError):
    """Raised when the MCP server reports that a tool execution failed."""


class MCPClient:
    """Async MCP stdio client for connecting to code-review-graph server."""

    def __init__(
        self,
        repo_root: Optional[str] = None,
        binary_path: Optional[str] = None,
        stdio_buffer_limit: Optional[int] = None,
    ):
        self.repo_root = repo_root
        self.binary_path = (
            binary_path
            or os.getenv("CRG_BINARY_PATH")
            or shutil.which("code-review-graph")
            or "/home/lupca/.local/bin/code-review-graph"
        )
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stdio_buffer_limit = stdio_buffer_limit or _stdio_buffer_limit()
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Start the code-review-graph serve process and run the initial MCP handshake."""
        if self.process and self.process.returncode is None:
            return

        if not self.binary_path or not os.path.exists(self.binary_path):
            raise MCPClientError(f"code-review-graph binary not found at: {self.binary_path}")

        cmd = [self.binary_path, "serve"]
        if self.repo_root:
            cmd.extend(["--repo", self.repo_root])

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=self.stdio_buffer_limit,
            )
        except Exception as e:
            raise MCPClientError(f"Failed to spawn MCP server process ({cmd}): {e}") from e

        # Handshake: initialize request
        try:
            init_res = await self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agmx", "version": "1.0"},
                },
                timeout=10.0,
            )
            if not init_res:
                raise MCPClientError("Initialize request returned empty response")

            # Notification: initialized
            await self._notify("notifications/initialized")
        except Exception as e:
            await self.close()
            raise MCPClientError(f"MCP handshake failed: {e}") from e

    async def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if not self.process or self.process.stdin is None:
            return
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        payload = (json.dumps(msg) + "\n").encode("utf-8")
        self.process.stdin.write(payload)
        await self.process.stdin.drain()

    async def _request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[Dict[str, Any]]:
        async with self._lock:
            if not self.process or self.process.stdout is None or self.process.stdin is None:
                raise MCPClientError("MCP process is not connected")

            req_id = await self._next_id()
            msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params

            payload = (json.dumps(msg) + "\n").encode("utf-8")
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

            try:
                line = await asyncio.wait_for(self.process.stdout.readline(), timeout=timeout)
                if not line:
                    raise MCPClientError("MCP server process closed stdio connection (EOF)")
                response = json.loads(line.decode("utf-8").strip())
                if "error" in response:
                    error = response["error"]
                    logger.warning("MCP response error for method '%s': %s", method, error)
                    if isinstance(error, dict):
                        message = str(error.get("message") or error)
                    else:
                        message = str(error)
                    raise MCPToolError(f"MCP method '{method}' failed: {message}")
                return response.get("result")
            except asyncio.TimeoutError:
                logger.error("MCP request '%s' timed out after %.1f seconds", method, timeout)
                raise MCPClientError(f"Request '{method}' timed out after {timeout}s")
            except json.JSONDecodeError as e:
                logger.error("MCP response JSON decode error: %s", e)
                raise MCPTransportError(f"Invalid JSON from MCP server: {e}")
            except ValueError as e:
                if "Separator is not found" in str(e) or "chunk exceed the limit" in str(e):
                    raise MCPTransportError(
                        "MCP response exceeded the stdio buffer limit "
                        f"({self.stdio_buffer_limit} bytes): {e}"
                    ) from e
                raise

    async def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        raise_on_error: bool = False,
    ) -> Optional[Any]:
        """Call an MCP tool on code-review-graph server with safe fallback."""
        try:
            if not self.process or self.process.returncode is not None:
                await self.connect()

            res = await self._request(
                "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                timeout=timeout,
            )
            if not res:
                return None

            if res.get("isError"):
                content = res.get("content", [])
                detail = ""
                if content and isinstance(content[0], dict):
                    detail = str(content[0].get("text") or "")
                raise MCPToolError(detail or f"MCP tool '{tool_name}' failed")

            content = res.get("content", [])
            if not content:
                return None

            first_text = content[0].get("text", "")
            if not first_text:
                return None

            try:
                return json.loads(first_text)
            except (json.JSONDecodeError, TypeError):
                return first_text
        except Exception as e:
            logger.warning("MCP tool '%s' execution failed: %s", tool_name, e)
            if raise_on_error:
                if isinstance(e, MCPClientError):
                    raise
                raise MCPTransportError(
                    f"MCP transport failed while calling '{tool_name}': {e}"
                ) from e
            return None

    async def close(self) -> None:
        """Close the MCP process cleanly."""
        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                    await self.process.stdin.wait_closed()
            except Exception:
                pass
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


def _stdio_buffer_limit() -> int:
    """Return the stdio line limit, configurable from the root environment."""
    configured = os.getenv("CRG_STDIO_BUFFER_LIMIT")
    if configured is None:
        return DEFAULT_STDIO_BUFFER_LIMIT
    try:
        limit = int(configured)
    except ValueError:
        logger.warning(
            "Ignoring invalid CRG_STDIO_BUFFER_LIMIT=%r; using %d",
            configured,
            DEFAULT_STDIO_BUFFER_LIMIT,
        )
        return DEFAULT_STDIO_BUFFER_LIMIT
    if limit < 64 * 1024:
        logger.warning(
            "Ignoring CRG_STDIO_BUFFER_LIMIT below 64 KiB: %d; using %d",
            limit,
            DEFAULT_STDIO_BUFFER_LIMIT,
        )
        return DEFAULT_STDIO_BUFFER_LIMIT
    return limit
