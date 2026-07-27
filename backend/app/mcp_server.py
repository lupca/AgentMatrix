"""FastMCP stdio projection of the tool registry (ADR-001 §D5).

The coordinator chat CLI path (claude/codex/agy spawned per turn instead of
calling the OpenAI-compatible API) has no way to execute Control Tower
tools. This process gives it one: every :class:`~app.services.tool_registry
.ToolSpec` becomes an MCP tool whose handler calls the Control Tower REST
API (``POST /api/mcp/tools/call``) with a scoped bearer token. Permission
and gate enforcement happen server-side in that endpoint (the same
``CommandRouter.execute_tool`` API mode uses), so a CLI can never bypass
four-eyes locally — this process only ever forwards arguments over HTTP.

This is a projection, not a second source of truth: tool names, schemas,
and descriptions all come straight from ``TOOL_REGISTRY``.

Run as:
    python -m app.mcp_server --api-url http://localhost:8000 --token <token>

Both flags default to the ``CT_API_URL`` / ``CT_MCP_TOKEN`` environment
variables so a generated MCP config (see
:func:`app.services.cli_dispatcher.build_mcp_config`) can supply them via
its ``env`` block instead of the command line.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

from app.services.tool_registry import ToolSpec, get_mcp_tool_specs

DEFAULT_API_URL = "http://localhost:8000"
REQUEST_TIMEOUT_SECONDS = 120.0


def make_tool_handler(*, api_url: str, token: str, tool_name: str, session_id: str):
    """Build the MCP handler for one registry tool.

    The handler is a thin HTTP forward: it does no local validation or
    interpretation of ``kwargs`` beyond passing them through as the tool's
    ``arguments`` object, so the server side stays the single place that
    understands tool semantics.
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=api_url, timeout=REQUEST_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    "/api/mcp/tools/call",
                    json={
                        "tool": tool_name,
                        "arguments": kwargs,
                        "session_id": session_id,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            return {"error": f"{tool_name} request failed: {exc}"}

        if response.status_code != 200:
            return {
                "error": (
                    f"{tool_name} failed: HTTP {response.status_code} "
                    f"{response.text}"
                )
            }
        return response.json()

    return handler


def register_tool(mcp: FastMCP, spec: ToolSpec, *, api_url: str, token: str, session_id: str) -> None:
    mcp.add_tool(
        FunctionTool(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            fn=make_tool_handler(
                api_url=api_url,
                token=token,
                tool_name=spec.name,
                session_id=session_id,
            ),
        )
    )


def build_server(*, api_url: str, token: str, session_id: str) -> FastMCP:
    mcp: FastMCP = FastMCP("control-tower")
    for spec in get_mcp_tool_specs():
        register_tool(mcp, spec, api_url=api_url, token=token, session_id=session_id)
    return mcp


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Control Tower MCP projection for the coordinator chat CLI"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("CT_API_URL", DEFAULT_API_URL),
        help="Base URL of the Control Tower REST API",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CT_MCP_TOKEN", ""),
        help="Scoped bearer token for POST /api/mcp/tools/call",
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("CT_MCP_SESSION_ID", "mcp-cli"),
        help="session_id attached to tool calls made through this server",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if not args.token:
        raise SystemExit(
            "A scoped token is required: pass --token or set CT_MCP_TOKEN."
        )
    server = build_server(
        api_url=args.api_url, token=args.token, session_id=args.session_id
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
