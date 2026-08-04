"""Native streamable-HTTP MCP server for Control Tower.

This is the only server surface: the old stdio -> REST forwarder
(``app.mcp_server``) and the FastAPI layer were removed in GD4 P1.
Native handlers resolve a DB session and call
``CommandRouter.execute_tool`` in-process; the router therefore remains the
single enforcement point for lifecycle and four-eyes rules.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.function_tool import FunctionTool

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.runtime_version import RuntimeVersionMonitor
from app.db.base import SessionLocal
from app.db.models import AdminGateRecord, GateRecord, Session as SessionModel, Task
from app.graph.context import invalidate_context_snapshot
from app.services.command_router import CommandRouter
from app.services.tool_registry import ToolSpec, get_mcp_tool_specs

# Injected into every connecting CLI's system prompt at initialize (Claude
# Code, Codex, agy all honour the MCP `instructions` field). Constraints:
# keep the whole text under 2KB (Claude Code truncates) and make the first
# ~512 characters self-contained (Codex's effective window).
SERVER_INSTRUCTIONS = (
    "AGMX task orchestration. Your role comes from your token: a "
    "coordinator token makes you the orchestrator (create, dispatch, review, "
    "approve); an executor token is scoped to one task — work it and report, "
    "coordinator-only tools will reject you. These tools are the ONLY interface: "
    "never read or modify AGMX's source code, database, .env, or "
    "processes via shell — a missing capability is a feature request for the "
    "human, not permission to bypass; report errors instead of patching the "
    "platform. Tasks flow todo > dispatched > awaiting-review > in-review > "
    "done, enforced server-side with four-eyes review (reviewer != executor) "
    "and approval gates. Follow the `next` field in every tool result; call "
    "get_status when unsure. In supervised mode a pending gate needs the "
    "human's explicit approval in chat before you call approve_gate. "
    "Read with query_db/get_status/get_stats/get_task_events/get_run_output; "
    "after dispatching, block on wait_for_task instead of polling on a timer; "
    "act with create_task, generate_spec_plan, suggest_agents, dispatch_task, "
    "request_review, record_verdict, approve_gate, cancel_task, archive_task, "
    "update_task; capture raw ideas with manage_inbox (add/list/promote) — ideas "
    "are free text, no gate; admin via manage_project/manage_agent/manage_knowledge/"
    "update_settings (a pending admin gate returns 'admin:<id>' — pass it to "
    "approve_gate). If a result carries pending_approvals, restate them to "
    "the human at the END of every reply, as a question, until each is "
    "decided. If a result carries runtime_warning, report it verbatim and "
    "never restart processes yourself. Errors are structured with a hint: "
    "follow the hint, do "
    "not retry blindly. The verdict belongs to the reviewer: never record a "
    "verdict for a review you did not run, never merge ct-run/* branches "
    "yourself, and report the task status from get_status verbatim — a "
    "failed task is failed even if the diff looks right."
)

TOKEN_PREFIX = "ct1"
ROLES = {"coordinator", "executor"}
TOKEN_PATTERN = re.compile(r"^ct1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")


@dataclass(frozen=True)
class TokenClaims:
    role: str
    task_id: str | None = None
    token_id: str | None = None
    session_id: str | None = None
    exp: int = 0


def _b64_json(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def issue_token(
    secret: str, *, role: str = "coordinator", task_id: str | None = None,
    token_id: str | None = None, session_id: str | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Issue a compact HMAC-signed token for the native MCP endpoint."""

    if role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    if not secret:
        raise ValueError("secret is required")
    ttl = ttl_seconds if ttl_seconds is not None else (3600 if role == "coordinator" else 900)
    if ttl <= 0:
        raise ValueError("ttl_seconds must be greater than zero")
    # Session.id is String(36); "mcp-" + 16 hex chars stays well under it
    # (a full uuid4 is 36 chars and used to overflow the column on Postgres).
    token_id = token_id or f"mcp-{uuid.uuid4().hex[:16]}"
    session_id = session_id or token_id
    payload = {"role": role, "token_id": token_id, "session_id": session_id, "exp": int(time.time()) + ttl}
    if task_id:
        payload["task_id"] = task_id
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
    if payload.get("role") not in ROLES or not payload.get("token_id") or not payload.get("session_id"):
        return None
    try:
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    return TokenClaims(
        role=payload["role"], task_id=payload.get("task_id"),
        token_id=payload["token_id"], session_id=payload["session_id"], exp=int(payload["exp"]),
    )


def _claims_from_request(default_token: str = "") -> TokenClaims | None:
    """Resolve token claims for the current tool call.

    ``get_http_headers()`` reads the active request from fastmcp's context
    var, so handlers don't need a ``Context`` parameter — a plain
    ``FunctionTool`` constructed with an explicit JSON schema never receives
    one (fastmcp only injects Context for introspected signatures). Outside
    an HTTP request (in-process transport) it returns ``{}`` and the
    ``default_token`` fallback applies.
    """

    headers: Mapping[str, str] = {}
    try:
        # get_http_headers() strips `authorization` by default; it must be
        # explicitly included — it is the whole point of this call.
        headers = get_http_headers(include={"authorization"}) or {}
    except Exception:
        headers = {}
    authorization = headers.get("authorization")
    return authenticate_token(
        authorization or default_token,
        secret=settings.MCP_TOKEN_SECRET,
    )


def _task_scope_arguments(
    claims: TokenClaims,
    spec: ToolSpec,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the executor's task scope to arguments where the schema supports it.

    A task-scoped executor must not be forced to repeat its scope for tools
    whose schema has no ``task_id`` field.  For schemas with an optional
    ``task_id``, omission means the task carried by the token.
    """

    scoped = dict(arguments)
    if claims.role != "executor":
        return scoped

    properties = spec.parameters.get("properties", {})
    if not isinstance(properties, Mapping) or "task_id" not in properties:
        return scoped

    required = spec.parameters.get("required", ())
    if (
        "task_id" not in required
        and not scoped.get("task_id")
        and claims.task_id
    ):
        scoped["task_id"] = claims.task_id
    return scoped


def _task_scope_ok(
    claims: TokenClaims,
    spec: ToolSpec,
    arguments: Mapping[str, Any],
) -> bool:
    if claims.role != "executor":
        return True

    properties = spec.parameters.get("properties", {})
    if not isinstance(properties, Mapping) or "task_id" not in properties:
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
        "dispatched": "Gọi wait_for_task để chờ executor xong và nhận kết quả trong một lần gọi.",
        "awaiting-review": "Gọi request_review để bắt đầu review độc lập.",
        "in-review": "Gọi wait_for_task để chờ verdict của reviewer.",
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


def _pending_approvals(db) -> list[dict[str, Any]]:
    """Every open human decision, attached to every tool result.

    A pending gate mentioned once and then buried under later reports gets
    forgotten — the human never learns they owe a decision. Surfacing the
    open set server-side means the coordinator cannot drop it.
    """
    pending: list[dict[str, Any]] = []
    try:
        # Both ledgers are append-only: a decision is a CHILD row pointing at
        # the pending row via parent_id, the parent keeps status="pending"
        # forever. "Still open" therefore means pending AND childless.
        decided_task = db.query(GateRecord.parent_id).filter(
            GateRecord.parent_id.isnot(None)
        )
        for row in (
            db.query(GateRecord)
            .join(Task, Task.id == GateRecord.task_id)
            .filter(
                GateRecord.status == "pending",
                GateRecord.id.notin_(decided_task),
                Task.archived_at.is_(None),
                # A gate on a finished task is moot — e.g. the driver's
                # auto re-dispatch gate orphaned when the replan went a
                # different way and the task still reached done (CTV2-246).
                Task.status.notin_(["done", "cancelled"]),
            )
            .order_by(GateRecord.created_at.asc())
            .limit(5)
        ):
            entry = {
                "id": row.task_id,
                "kind": f"task:{row.gate_type}",
                "waiting_since": row.created_at.isoformat() if row.created_at else None,
            }
            if row.gate_type == "review_order":
                payload = row.input_payload or {}
                entry["prompt"] = payload.get("approval_prompt")
            pending.append(entry)
        decided_admin = db.query(AdminGateRecord.parent_id).filter(
            AdminGateRecord.parent_id.isnot(None)
        )
        for row in (
            db.query(AdminGateRecord)
            .filter(
                AdminGateRecord.status == "pending",
                AdminGateRecord.id.notin_(decided_admin),
            )
            .order_by(AdminGateRecord.created_at.asc())
            .limit(5)
        ):
            pending.append({
                "id": f"admin:{row.id}",
                "kind": f"admin:{row.entity}/{row.action}",
                "waiting_since": row.created_at.isoformat() if row.created_at else None,
            })
        # Escalations (safety brake, invalid review result) raise
        # awaiting_approval WITHOUT a gate record (CTV2-221). In auto mode
        # these are the only human decisions there are — without this branch
        # the reminder list stays empty exactly when a human is needed most.
        gated_tasks = {p["id"] for p in pending}
        for row in (
            db.query(Task)
            .filter(
                Task.awaiting_approval.is_(True),
                Task.archived_at.is_(None),
            )
            .order_by(Task.updated_at.asc())
            .limit(5)
        ):
            if row.id in gated_tasks:
                continue
            pending.append({
                "id": row.id,
                "kind": "task:escalation",
                "prompt": (row.approval_prompt or "")[:160] or None,
                "waiting_since": row.updated_at.isoformat() if row.updated_at else None,
            })
    except Exception:  # a broken reminder must never break the tool call
        return []
    return pending


def _ensure_session(db, claims: TokenClaims) -> str:
    """Give every native token a real router session, including executor tokens."""
    session_id = claims.session_id or claims.token_id or "mcp"
    if db.get(SessionModel, session_id) is not None:
        return session_id
    task = db.get(Task, claims.task_id) if claims.task_id else None
    db.add(SessionModel(
        id=session_id, thread_id=session_id, title=f"MCP {claims.role}",
        context_level="task" if task else "global",
        project_id=task.project if task else None,
        task_id=task.id if task else None, messages=[], status="active",
    ))
    try:
        db.commit()
    except IntegrityError:
        # A concurrent first call with the same token already created the
        # row between our existence check and this commit.
        db.rollback()
    return session_id


def make_tool_handler(
    spec: ToolSpec,
    *,
    default_token: str = "",
    runtime_version: RuntimeVersionMonitor | None = None,
):
    async def handler(**kwargs: Any) -> dict[str, Any]:
        claims = _claims_from_request(default_token)
        if claims is None:
            return {"ok": False, "data": None, "error": {"code": "unauthorized", "message": "Invalid or missing MCP token"}}
        if spec.required_role == "coordinator" and claims.role != "coordinator":
            return {"ok": False, "data": None, "error": {"code": "forbidden", "message": "This tool requires a coordinator token"}}
        scoped_kwargs = _task_scope_arguments(claims, spec, kwargs)
        if not _task_scope_ok(claims, spec, scoped_kwargs):
            return {"ok": False, "data": None, "error": {"code": "task_scope_violation", "message": "Executor token is scoped to a different task"}}
        db = SessionLocal()
        try:
            session_id = _ensure_session(db, claims)
            result = await CommandRouter(db).execute_tool(spec.name, scoped_kwargs, session_id)
            if (
                runtime_version is not None
                and spec.name in {"get_status", "land_task"}
                and not result.get("error")
            ):
                warning = runtime_version.stale_warning()
                if warning is not None:
                    result = {**result, "runtime_warning": warning}
            # Native calls bypass the REST endpoint, so invalidate the same
            # context cache the old /api/mcp/tools/call path invalidated.
            invalidate_context_snapshot(db, project_id=None)
            response = envelope(result, next_step=_next_step(result))
            pending = _pending_approvals(db)
            if pending:
                response["pending_approvals"] = pending
                response["pending_approvals_note"] = (
                    "Các gate này đang CHỜ human quyết định — nhắc lại cho "
                    "human ở CUỐI mỗi câu trả lời (kèm câu hỏi approve?) cho "
                    "đến khi chúng được approve/reject qua approve_gate."
                )
            return response
        except Exception as exc:  # boundary: MCP must always return structured JSON
            return {"ok": False, "data": None, "error": {"code": "internal_error", "message": str(exc)}}
        finally:
            db.close()

    return handler


class RoleFilteredFastMCP(FastMCP):
    """FastMCP projection whose tool list follows the connecting token.

    FastMCP invokes ``list_tools`` for every ``tools/list`` request, while the
    request context still contains the incoming HTTP headers.  Filtering here
    therefore gives each MCP connection its role-specific schema list without
    creating separate servers or endpoints.  The handler-level role check in
    :func:`make_tool_handler` remains the security boundary.
    """

    def __init__(
        self,
        *args: Any,
        tool_specs: list[ToolSpec],
        default_token: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._mcp_tool_specs = {spec.name: spec for spec in tool_specs}
        self._default_token = default_token

    async def list_tools(self, *, run_middleware: bool = True):
        claims = _claims_from_request(self._default_token)
        tools = await super().list_tools(run_middleware=run_middleware)
        if claims is None:
            return []
        if claims.role == "coordinator":
            return tools
        return [
            tool
            for tool in tools
            if self._mcp_tool_specs[tool.name].required_role == "executor"
        ]


def build_server(
    *,
    default_token: str = "",
    runtime_version: RuntimeVersionMonitor | None = None,
) -> FastMCP:
    # Capture once while constructing the process' MCP application.  Every
    # get_status/land_task call compares against this immutable boot SHA.
    runtime_version = runtime_version or RuntimeVersionMonitor.capture()
    specs = get_mcp_tool_specs()
    mcp = RoleFilteredFastMCP(
        "agmx",
        instructions=SERVER_INSTRUCTIONS,
        tool_specs=specs,
        default_token=default_token,
    )
    for spec in specs:
        mcp.add_tool(FunctionTool(
            name=spec.name,
            description=spec.description + " Follow the precondition and the `next` field in every result.",
            parameters=spec.parameters,
            fn=make_tool_handler(
                spec,
                default_token=default_token,
                runtime_version=runtime_version,
            ),
        ))
    return mcp


def build_http_app(
    *,
    default_token: str = "",
    runtime_version: RuntimeVersionMonitor | None = None,
):
    """Build the standalone ASGI app and reject unauthenticated HTTP calls."""

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    server = build_server(
        default_token=default_token,
        runtime_version=runtime_version,
    )
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
    if not settings.MCP_TOKEN_SECRET:
        raise SystemExit("MCP_TOKEN_SECRET is required")
    import uvicorn

    uvicorn.run(build_http_app(default_token=args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
