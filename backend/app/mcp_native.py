"""Native streamable-HTTP MCP server for Control Tower.

This module is deliberately separate from :mod:`app.mcp_server`.  The latter
is the old stdio -> REST forwarder and remains the rollback path while clients
move to this server.  Native handlers resolve a DB session and call
``CommandRouter.execute_tool`` in-process; the router therefore remains the
single enforcement point for lifecycle and four-eyes rules.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import inspect
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from fastmcp import Context, FastMCP
from fastmcp.tools.function_tool import FunctionTool

from app.core.config import settings
from app.db.base import SessionLocal
from app.graph.context import invalidate_context_snapshot
from app.services.command_router import CommandRouter
from app.services.tool_registry import ToolSpec, get_mcp_tool_specs

TOKEN_PREFIX = "ct1"
ROLES = {"coordinator", "executor"}
TOKEN_PATTERN = re.compile(r"^ct1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")


@dataclass(frozen=True)
class TokenClaims:
    role: str
    task_id: str | None = None
    token_id: str | None = None


def _b64_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def issue_token(
    secret: str, *, role: str = "coordinator", task_id: str | None = None,
    token_id: str | None = None,
) -> str:
    """Issue a compact HMAC-signed token for the native MCP endpoint."""

    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    payload = {"role": role}
    if task_id:
        payload["task_id"] = task_id
    if token_id:
        payload["token_id"] = token_id
    encoded = _b64_json(payload)
    signature = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{TOKEN_PREFIX}.{encoded}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def authenticate_token(
    presented: str | None, *, secret: str
) -> TokenClaims | None:
    """Validate a signed HMAC token."""

    value = (presented or "").removeprefix("Bearer ").strip()
    if not value:
        return None
    match = TOKEN_PATTERN.match(value)
    if not match or not secret:
        return None
    encoded, provided = match.groups()
    expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
    try:
        actual = base64.urlsafe_b64decode(provided + "=" * (-len(provided) % 4))
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not hmac.compare_digest(actual, expected):
        return None
    if payload.get("role") not in ROLES:
        return None
    return TokenClaims(
        role=payload["role"], task_id=payload.get("task_id"), token_id=payload.get("token_id")
    )


async def _claims_from_context(context: Context | None, default_token: str = "") -> TokenClaims | None:
    headers: Mapping[str, str] = {}
    if context is not None:
        try:
            headers = context.get_http_headers()  # fastmcp >= 3
            if inspect.isawaitable(headers):
                headers = await headers
        except (AttributeError, TypeError):
            try:
                headers = context.request_context.request.headers
            except AttributeError:
                headers = {}
    authorization = headers.get("authorization") or headers.get("Authorization")
    return authenticate_token(
        authorization or default_token,
        secret=settings.MCP_TOKEN_SECRET,
    )


def _task_scope_ok(claims: TokenClaims, arguments: Mapping[str, Any]) -> bool:
    if claims.role != "executor":
        return True
    requested = arguments.get("task_id")
    return bool(claims.task_id and requested and str(requested) == claims.task_id)


def _error_code(message: str) -> str:
    text = message.lower()
    if "already" in text or "expected status" in text or "conflict" in text:
        return "task_transition_conflict"
    if "not found" in text:
        return "not_found"
    if "required" in text or "invalid" in text:
        return "invalid_arguments"
    if "gate" in text:
        return "gate_rejected"
    return "tool_rejected"


def _next_step(result: Mapping[str, Any]) -> str | None:
    task = result.get("task")
    if not isinstance(task, Mapping):
        if result.get("action") == "created":
            return "Gọi generate_spec_plan cho task mới, sau đó dispatch_task."
        return None
    status = task.get("status")
    return {
        "todo": "Gọi generate_spec_plan nếu task chưa có plan, sau đó dispatch_task.",
        "dispatched": "Chờ executor hoàn tất; gọi get_status để theo dõi.",
        "awaiting-review": "Gọi request_review để bắt đầu review độc lập.",
        "in-review": "Chờ reviewer; gọi get_status để theo dõi verdict.",
        "changes-requested": "Gọi dispatch_task để chạy lại task sau khi cập nhật.",
        "done": "Task đã done; không cần gọi thêm transition.",
    }.get(str(status))


def envelope(result: Mapping[str, Any], *, next_step: str | None = None) -> dict[str, Any]:
    """Normalize router output without exposing raw transition errors."""

    if result.get("error"):
        raw = str(result["error"])
        error: dict[str, Any] = {"code": _error_code(raw), "message": raw}
        if "already" in raw.lower() or "expected status" in raw.lower():
            error["hint"] = "Gọi get_status để xem trạng thái mới rồi làm theo trường next."
        return {"ok": False, "data": None, "error": error}
    return {"ok": True, "data": dict(result), **({"next": next_step} if next_step else {})}


def make_tool_handler(spec: ToolSpec, *, default_token: str = ""):
    async def handler(context: Context, **kwargs: Any) -> dict[str, Any]:
        claims = await _claims_from_context(context, default_token)
        if claims is None:
            return {"ok": False, "data": None, "error": {"code": "unauthorized", "message": "Invalid or missing MCP token"}}
        if spec.required_role == "coordinator" and claims.role != "coordinator":
            return {"ok": False, "data": None, "error": {"code": "forbidden", "message": "This tool requires a coordinator token"}}
        if not _task_scope_ok(claims, kwargs):
            return {"ok": False, "data": None, "error": {"code": "task_scope_violation", "message": "Executor token is scoped to a different task"}}
        db = SessionLocal()
        try:
            result = await CommandRouter(db).execute_tool(spec.name, kwargs, claims.token_id or "mcp")
            # Native calls bypass the REST endpoint, so invalidate the same
            # context cache the old /api/mcp/tools/call path invalidated.
            invalidate_context_snapshot(db, project_id=None)
            return envelope(result, next_step=_next_step(result))
        except Exception as exc:  # boundary: MCP must always return structured JSON
            return {"ok": False, "data": None, "error": {"code": "internal_error", "message": str(exc)}}
        finally:
            db.close()

    return handler


def build_server(*, default_token: str = "") -> FastMCP:
    mcp = FastMCP("control-tower")
    for spec in get_mcp_tool_specs():
        mcp.add_tool(FunctionTool(
            name=spec.name,
            description=spec.description + " Follow the precondition and the `next` field in every result.",
            parameters=spec.parameters,
            fn=make_tool_handler(spec, default_token=default_token),
        ))
    return mcp


def build_http_app(*, default_token: str = ""):
    """Build the standalone ASGI app and reject unauthenticated HTTP calls."""

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    server = build_server(default_token=default_token)
    app = server.http_app()

    async def health(_request):
        return JSONResponse({"status": "ok"})

    app.add_route("/health", health, methods=["GET"])

    class MCPAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path.rstrip("/") == "/health":
                return await call_next(request)
            claims = authenticate_token(
                request.headers.get("authorization") or (default_token if default_token else None),
                secret=settings.MCP_TOKEN_SECRET,
            )
            if claims is None:
                return JSONResponse(
                    {"ok": False, "error": {"code": "unauthorized", "message": "Invalid or missing MCP token"}},
                    status_code=401,
                )
            return await call_next(request)

    app.add_middleware(MCPAuthMiddleware)
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control Tower native streamable HTTP MCP server")
    parser.add_argument("--host", default=os.environ.get("CT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CT_MCP_PORT", "8100")))
    parser.add_argument("--token", default=os.environ.get("CT_MCP_TOKEN", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    import uvicorn

    uvicorn.run(build_http_app(default_token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
