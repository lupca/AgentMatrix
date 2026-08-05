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
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.function_tool import FunctionTool

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.runtime_version import RuntimeVersionMonitor
from app.db.base import SessionLocal
from app.db.models import (
    AdminGateRecord,
    GateRecord,
    Session as SessionModel,
    Task,
    TaskEvent,
    TaskOwner,
)
from app.graph.context import invalidate_context_snapshot
from app.services.command_router import CommandRouter
from app.services.task_state_machine import (
    TaskStateMachine,
    build_gate_brief,
    verdict_ac_checks,
)
from app.services.tool_argument_validator import (
    describe_problems,
    validate_tool_arguments,
)
from app.services.task_event_service import TaskEventService
from app.services.tool_registry import ToolSpec, get_mcp_tool_specs

# Injected into every connecting CLI's system prompt at initialize (Claude
# Code, Codex, agy all honour the MCP `instructions` field). Constraints:
# keep the whole text under 2KB (Claude Code truncates) and make the first
# ~512 characters self-contained (Codex's effective window).
SERVER_INSTRUCTIONS = (
    "AGMX task orchestration. Your token sets your role: a coordinator token "
    "makes you the orchestrator; an executor token is scoped to one task. "
    "As coordinator you decide -- a gate is where you stop and check the "
    "evidence, then call approve_gate yourself. Not enough to decide? Ask the "
    "human and do NOT approve; see `unknowns` in the gate brief. You may edit "
    "the repo you coordinate (its `repo_root`), and tasks are the record of "
    "work, not a permission queue. Never bends: four-eyes on code (an "
    "independent reader before main), the verdict belongs to whoever ran the "
    "review, GateRecord is append-only. Tasks flow todo > dispatched > "
    "awaiting-review > in-review > done. Follow the `next` field in every "
    "result and the tool named in every error -- they say what to do next. "
    "Call get_status when unsure; after dispatching, block on wait_for_task. "
    "Read the spec before deciding anything unusual: spec_get is the truth, "
    "anchored to code. Read with query_db/get_status/get_stats/"
    "get_task_events/get_run_output; act with create_task/generate_spec_plan/"
    "dispatch_task/request_review/record_verdict/approve_gate/reopen_task/"
    "cancel_task/update_task; capture ideas with manage_inbox (no gate); admin "
    "via manage_project/manage_agent/manage_knowledge/update_settings (a "
    "pending admin gate returns 'admin:<id>' -- pass it to approve_gate). "
    "Report task status from get_status verbatim: a failed task is failed even "
    "if the diff looks right."
)

TOKEN_PREFIX = "ct1"
ROLES = {"coordinator", "executor"}
TOKEN_PATTERN = re.compile(r"^ct1\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")

# VIỆC 2 (CTV2-1399): only tools that CHANGE a task's state register the
# calling session as its owner. Read-only tools (get_status, query_db, ...)
# never do -- looking at a task must not steal it from whoever is working it.
REGISTERING_TOOLS = frozenset({
    "create_task", "update_task", "dispatch_task", "request_review",
    "approve_gate", "record_verdict", "land_task", "reopen_task",
    "cancel_task", "spec_write",
})

# A session that hasn't been seen in this long no longer counts as an owner:
# its claim is orphaned and the task must be visible to every session again
# (VIỆC 2, "kết nối là phù du").
OWNER_STALE_SECONDS = 900

# VIỆC 3: event types that belong in the "việc hỏng" (broken work) group --
# they persist until someone acts on them, unlike "việc xong" which is
# read-once. Kept local to this module (not DECISION_EVENT_TYPES in
# task_event_service) so it does not change what notification_dispatcher
# sends over Telegram -- that channel is untouched by this change.
FAILED_EVENT_TYPES = frozenset({"run_failed", "landing_failed", "dispatch_no_run"})
DONE_EVENT_TYPES = frozenset({"done", "landed", "spec_plan_completed", "plan_critic_completed"})

_NEXT_BY_EVENT_TYPE = {
    "run_failed": "xem get_run_output rồi dispatch_task lại sau khi sửa, hoặc reopen_task",
    "landing_failed": "áp diff tay hoặc rebase rồi gọi land_task lại",
    "dispatch_no_run": "gọi dispatch_task lại, hoặc kiểm tra agent/worker",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _current_http_headers() -> Mapping[str, str]:
    """Read the active request's headers, ``authorization`` and
    ``mcp-session-id`` included — both are stripped by default.

    Outside an HTTP request (in-process transport, tests) this returns
    ``{}``.
    """

    try:
        return get_http_headers(include={"authorization", "mcp-session-id"}) or {}
    except Exception:
        return {}


def _claims_from_request(default_token: str = "") -> TokenClaims | None:
    """Resolve token claims for the current tool call.

    ``get_http_headers()`` reads the active request from fastmcp's context
    var, so handlers don't need a ``Context`` parameter — a plain
    ``FunctionTool`` constructed with an explicit JSON schema never receives
    one (fastmcp only injects Context for introspected signatures). Outside
    an HTTP request (in-process transport) it returns ``{}`` and the
    ``default_token`` fallback applies.
    """

    headers = _current_http_headers()
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
    if not spec.infer_task_scope:
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
    if not requested and not spec.infer_task_scope:
        return True
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
    """Say what to do next, derived from the WHOLE state -- not one column.

    This used to be a lookup on `status` alone, which cannot tell apart three
    very different situations that all read 'todo': nothing has started, a
    planner is running right now, and a gate is waiting for a decision. It
    answered "call generate_spec_plan, then dispatch_task" to all three.

    On 2026-08-05 that advice was actively wrong for four voma tasks whose
    planners were mid-flight (CTV2-1404): the coordinator was told to start
    work that was already underway, and never told that waiting was the
    correct move or that `wait_for_task` is how you wait. A coordinator that
    does not know its next step is usually a system that told it the wrong one.
    """
    task = result.get("task")
    if not isinstance(task, Mapping):
        if result.get("action") == "created":
            return "Gọi generate_spec_plan cho task mới, sau đó dispatch_task."
        return None

    status = str(task.get("status") or "")
    gate = str(task.get("current_gate") or "")
    waiting_human = bool(task.get("awaiting_approval"))

    # A pending gate outranks the status: every transition tool will refuse
    # until it is decided, so pointing at one of them just wastes a call.
    if waiting_human and status not in {"done", "cancelled"}:
        return (
            f"Task đang chờ quyết định ở gate {gate!r}. Đọc gate brief qua get_status, "
            "kiểm chứng bằng chứng, rồi tự gọi approve_gate nếu không vấn đề — "
            "gate là chỗ dừng lại xác nhận, không phải chỗ xin phép. "
            "Các tool chuyển trạng thái sẽ bị từ chối cho tới khi gate được quyết."
        )

    if status == "todo":
        if gate in {"plan", "spec"}:
            return (
                "Planner đang chạy cho task này — gọi wait_for_task để chờ plan xong "
                "trong một lần gọi, đừng gọi lại generate_spec_plan (sẽ chạy trùng) "
                "và đừng poll get_status theo nhịp."
            )
        return "Gọi generate_spec_plan nếu task chưa có plan, sau đó dispatch_task."

    return {
        "dispatched": "Gọi wait_for_task để chờ executor xong và nhận kết quả trong một lần gọi.",
        "awaiting-review": "Gọi request_review để bắt đầu review độc lập.",
        "in-review": "Gọi wait_for_task để chờ verdict của reviewer.",
        "changes-requested": "Gọi dispatch_task để chạy lại task sau khi cập nhật.",
        "failed": "Gọi reopen_task để đưa task về trạng thái làm tiếp được.",
        "done": "Task đã done; không cần gọi thêm transition.",
    }.get(status)


def envelope(result: Mapping[str, Any], *, next_step: str | None = None) -> dict[str, Any]:
    """Normalize router output without exposing raw transition errors."""

    if result.get("error"):
        raw = str(result["error"])
        error: dict[str, Any] = {"code": _error_code(raw), "message": raw}
        if "already" in raw.lower() or "expected status" in raw.lower():
            error["hint"] = "Gọi get_status để xem trạng thái mới rồi làm theo trường next."
        return {"ok": False, "data": None, "error": error}
    return {"ok": True, "data": dict(result), **({"next": next_step} if next_step else {})}


# What the coordinator is expected to check before deciding each kind of gate.
# Named per gate type so the reminder is about THIS decision, not a generic
# "please verify" -- the note is attached to every tool result, so a vague one
# is noise the reader learns to skip.
_GATE_CHECKS = {
    "verdict": (
        "re-run the numbers the reviewer quoted, open the exact lines its "
        "findings point at, and read the body of any test it credits"
    ),
    "dispatch": (
        "confirm the plan matches the intent and the chosen executor suits the "
        "work"
    ),
    "review_order": (
        "confirm the reviewer is independent of the executor and suits the work"
    ),
    "escalation": (
        "read what blocked it and whether that condition still holds"
    ),
    "safety_brake": (
        "compare the recorded numbers against the limit, and check whether a "
        "result was already delivered"
    ),
}


# Tools whose result is itself a gate decision worth explaining in full --
# unlike `pending_approvals` (attached to every result), these are the one
# call that just created or decided the gate in question.
_GATE_RESULT_TOOLS = {"dispatch_task", "request_review", "record_verdict", "approve_gate"}


def _resolve_pending_gate_record(db, kwargs: Mapping[str, Any]) -> GateRecord | None:
    """Same lookup approve_gate itself uses: gate_record_id, else task_id's pending row."""
    raw_id = kwargs.get("gate_record_id") or kwargs.get("task_id")
    if not raw_id or str(raw_id).startswith("admin:"):
        return None
    try:
        return db.get(GateRecord, int(raw_id))
    except (TypeError, ValueError):
        return (
            db.query(GateRecord)
            .filter(GateRecord.task_id == str(raw_id), GateRecord.status == "pending")
            .order_by(GateRecord.id.desc())
            .first()
        )


def _verdict_evidence_block(db, kwargs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Refuse to approve a verdict gate with no evidence -- with a brief, not silence.

    Returns None when the call may proceed (not a verdict approval, evidence
    was supplied, or nothing pending to check against).
    """
    decision = str(kwargs.get("decision") or "approved").strip().lower()
    if decision not in {"approved", "approve", "yes", "y"}:
        return None
    if kwargs.get("evidence"):
        return None
    record = _resolve_pending_gate_record(db, kwargs)
    if record is None or record.gate_type != "verdict" or record.status != "pending":
        return None
    brief = build_gate_brief(db, record)
    checks = verdict_ac_checks(record) or [_GATE_CHECKS["verdict"]]
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": "evidence_required",
            "message": (
                "Approving a verdict gate requires evidence: the checks you "
                "actually ran, not a re-statement of the reviewer's claim."
            ),
            "hint": (
                "Call approve_gate again with evidence=[{check, result}, ...] "
                "covering the checks below."
            ),
        },
        "brief": brief["summary"],
        "unknowns": brief["unknowns"],
        "checks": checks,
    }


def _attach_gate_brief(db, tool_name: str, result: Mapping[str, Any], response: dict[str, Any]) -> None:
    """Give the ONE call that just made this gate decision the full brief.

    Not attached to every result (that's `pending_approvals`, summary-only) --
    only to the tool call that dispatched/ordered-review/recorded a verdict/
    approved a gate, where the full brief is exactly what was asked for.
    """
    if result.get("error") or tool_name not in _GATE_RESULT_TOOLS:
        return
    gate_record_id = result.get("gate_record_id")
    if not gate_record_id:
        return
    record = db.get(GateRecord, gate_record_id)
    if record is None:
        return
    brief = build_gate_brief(db, record)
    data = response.get("data")
    if isinstance(data, dict):
        data["gate_brief"] = brief["summary"]
        data["unknowns"] = brief["unknowns"]


def _persist_verdict_evidence(db, tool_name: str, kwargs: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    if tool_name != "approve_gate" or result.get("error"):
        return
    evidence = kwargs.get("evidence")
    gate_record_id = result.get("gate_record_id")
    if not evidence or not gate_record_id:
        return
    record = db.get(GateRecord, gate_record_id)
    if record is None or record.gate_type != "verdict":
        return
    TaskStateMachine(db).record_gate_evidence(gate_record_id, evidence)


def _gate_decider(db, row) -> tuple[str, str | None]:
    """Who decides this gate -- coordinator, human, or not determinable.

    Derived from the task's effective mode rather than asserted.  This used to
    be a hardcoded sentence -- "these gates are WAITING for the human, repeat
    them at the END of every reply until decided" -- appended to every tool
    result regardless of mode.  A coordinator session reads that line dozens of
    times per session, so it outweighed any instruction saying otherwise
    (CTV2-1391): the most repeated string in the system was also the one that
    contradicted the coordinator's actual authority.

    Three outcomes, not two.  The first cut fell back to "coordinator" when the
    lookup raised, which leans the wrong way: unable to tell whether a human is
    required, it would have said "this one is yours" and invited a
    self-approval on a task that wanted a human.  Failing silently toward the
    permissive answer is the worse half of the two.  Now an unresolved mode
    says so, and carries the reason -- catching an exception and returning an
    ordinary label is how a real fault gets swallowed.

    Returns (decider, reason_when_unknown).
    """
    try:
        from app.services.task_validators import TaskValidator

        task = db.get(Task, row.task_id)
        if task is None:
            return "unknown", f"task {row.task_id} not found"
        mode = TaskValidator(db).mode_for_task(task)
        return ("human" if mode == "supervised" else "coordinator"), None
    except Exception as exc:  # boundary: a projection must never break a tool result
        return "unknown", f"{type(exc).__name__}: {exc}"


def _pending_approvals_note(pending: list[dict[str, Any]]) -> str:
    """Say who decides, and -- when that is the coordinator -- what to check."""
    deciders = {entry.get("decided_by") for entry in pending}
    kinds = {
        str(entry.get("kind", "")).split(":", 1)[-1]
        for entry in pending
        if entry.get("decided_by") == "coordinator"
    }
    checks = [_GATE_CHECKS[kind] for kind in sorted(kinds) if kind in _GATE_CHECKS]

    parts: list[str] = []
    if "unknown" in deciders:
        # Say what could not be determined and why, rather than picking a side
        # quietly.  An unresolved mode is a fault worth surfacing, not a
        # default worth guessing.
        reasons = sorted(
            {
                str(entry.get("decider_unknown_reason"))
                for entry in pending
                if entry.get("decided_by") == "unknown"
                and entry.get("decider_unknown_reason")
            }
        )
        detail = f" ({'; '.join(reasons)})" if reasons else ""
        parts.append(
            "Could not determine who decides some of these gates" + detail + " -- "
            "treat them as needing the human, and look into why the lookup failed."
        )
    if "human" in deciders:
        parts.append(
            "Some need the human's decision (the task's mode says so) -- restate "
            "those at the END of every reply until they are decided."
        )
    if "coordinator" in deciders:
        note = (
            "The rest are yours to decide: verify the claims yourself, then call "
            "approve_gate. If a gate does not give you enough to decide, ask the "
            "human and do not approve it."
            if len(parts)
            else "These gates are yours to decide. Verify the claims yourself, "
            "then call approve_gate. If a gate does not give you enough to "
            "decide, ask the human and do not approve it."
        )
        if checks:
            note += " Worth checking here: " + "; ".join(checks) + "."
        parts.append(note)
    hidden = sum(
        int(entry.get("hidden_count") or 0)
        for entry in pending
        if entry.get("kind") == "meta:hidden"
    )
    if hidden:
        parts.append(f"({hidden} mục khác đang bị ẩn do giới hạn hiển thị.)")
    return " ".join(parts)


def _owner_visible_filter(db, session_id: str | None):
    """Task-id filter: owned by nobody, owned by a stale session, or owned by me.

    A task claimed by an ACTIVE other session is hidden here -- its news goes
    to its owner (VIỆC 2). Everything else (no owner, or the owner has not
    been seen in ``OWNER_STALE_SECONDS``) stays visible to everyone, because
    a connection is ephemeral and an orphaned claim must never make a task
    disappear (VIỆC 2, "lưới an toàn bắt buộc").
    """
    stale_before = _utcnow() - timedelta(seconds=OWNER_STALE_SECONDS)
    active_other_owner = (
        db.query(TaskOwner.task_id)
        .join(SessionModel, SessionModel.id == TaskOwner.session_id)
        .filter(
            TaskOwner.session_id.isnot(None),
            TaskOwner.session_id != (session_id or ""),
            SessionModel.last_activity_at >= stale_before,
        )
    )
    return Task.id.notin_(active_other_owner)


def _pending_approvals(
    db, *, session_id: str | None = None, project_scope: str | None = None
) -> list[dict[str, Any]]:
    """Every open human decision, attached to every tool result.

    A pending gate mentioned once and then buried under later reports gets
    forgotten — the human never learns they owe a decision. Surfacing the
    open set server-side means the coordinator cannot drop it.

    Filtered by PROJECT (the outer layer, VIỆC 4: a session scoped to
    agenticmatix never sees VOMA-008) and by ownership (VIỆC 2). Newest
    first, not oldest-first -- one gate stuck for hours must not bury
    everything that happened since (VIỆC 4).
    """
    pending: list[dict[str, Any]] = []
    hidden = 0
    try:
        # Both ledgers are append-only: a decision is a CHILD row pointing at
        # the pending row via parent_id, the parent keeps status="pending"
        # forever. "Still open" therefore means pending AND childless.
        decided_task = db.query(GateRecord.parent_id).filter(
            GateRecord.parent_id.isnot(None)
        )
        gate_query = (
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
                _owner_visible_filter(db, session_id),
            )
        )
        if project_scope:
            gate_query = gate_query.filter(Task.project == project_scope)
        total_gates = gate_query.count()
        gate_rows = list(gate_query.order_by(GateRecord.created_at.desc()).limit(5))
        hidden += max(0, total_gates - len(gate_rows))
        for row in gate_rows:
            decided_by, unknown_reason = _gate_decider(db, row)
            entry = {
                "id": row.task_id,
                "kind": f"task:{row.gate_type}",
                "waiting_since": row.created_at.isoformat() if row.created_at else None,
                "decided_by": decided_by,
            }
            if unknown_reason:
                entry["decider_unknown_reason"] = unknown_reason
            if row.gate_type == "review_order":
                payload = row.input_payload or {}
                entry["prompt"] = payload.get("approval_prompt")
            # Summary only, not the full brief -- every tool result carries
            # this list, so a full brief here is exactly the noise CTV2-1393
            # is fixing. unknowns is the one part worth repeating in full: it
            # is the thing most likely to change the decision.
            try:
                brief = build_gate_brief(db, row)
                entry["summary"] = brief["summary"][:200]
                if brief["unknowns"]:
                    entry["unknowns"] = brief["unknowns"]
            except Exception:
                pass
            pending.append(entry)
        decided_admin = db.query(AdminGateRecord.parent_id).filter(
            AdminGateRecord.parent_id.isnot(None)
        )
        # Admin gates aren't task/project-scoped -- always shown, unfiltered.
        admin_query = db.query(AdminGateRecord).filter(
            AdminGateRecord.status == "pending",
            AdminGateRecord.id.notin_(decided_admin),
        )
        total_admin = admin_query.count()
        admin_rows = list(admin_query.order_by(AdminGateRecord.created_at.desc()).limit(5))
        hidden += max(0, total_admin - len(admin_rows))
        for row in admin_rows:
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
        esc_query = db.query(Task).filter(
            Task.awaiting_approval.is_(True),
            Task.archived_at.is_(None),
            _owner_visible_filter(db, session_id),
        )
        if project_scope:
            esc_query = esc_query.filter(Task.project == project_scope)
        total_esc = esc_query.count()
        esc_rows = [
            row for row in esc_query.order_by(Task.updated_at.desc()).limit(5 + len(gated_tasks))
            if row.id not in gated_tasks
        ][:5]
        hidden += max(0, total_esc - len(gated_tasks) - len(esc_rows))
        for row in esc_rows:
            pending.append({
                "id": row.id,
                "kind": "task:escalation",
                "prompt": (row.approval_prompt or "")[:160] or None,
                "waiting_since": row.updated_at.isoformat() if row.updated_at else None,
            })
    except Exception:  # a broken reminder must never break the tool call
        return []
    if hidden:
        pending.append({"id": None, "kind": "meta:hidden", "hidden_count": hidden})
    return pending


def _register_task_ownership(
    db, spec_name: str, scoped_kwargs: Mapping[str, Any], result: Mapping[str, Any], session_id: str
) -> None:
    """VIỆC 2: whoever changes a task's state owns its future notifications.

    Registered at this ONE boundary (not inside every handler) so no tool
    can forget to claim the task it just changed. Last writer wins: a second
    session touching the same task simply overwrites the row.
    """
    if spec_name not in REGISTERING_TOOLS or result.get("error"):
        return
    task_id = None
    task_payload = result.get("task")
    if isinstance(task_payload, Mapping):
        task_id = task_payload.get("id")
    if not task_id:
        task_id = result.get("task_id") or scoped_kwargs.get("task_id")
    if not task_id:
        return
    task_id = str(task_id)
    row = db.get(TaskOwner, task_id)
    now = _utcnow()
    if row is None:
        db.add(TaskOwner(task_id=task_id, session_id=session_id, updated_at=now))
    else:
        row.session_id = session_id
        row.updated_at = now
    db.commit()


def _session_project_scope(db, session_id: str) -> str | None:
    """PROJECT is the outer scoping layer (VIỆC 4): a session that owns tasks
    only in project X never sees inbox items from project Y.

    Derived from the most recently touched owned task rather than stored on
    the ``Session`` row itself -- ``sessions.project_id`` is constrained by
    ``context_level`` (a global-level session must keep it NULL), and this
    avoids fighting that invariant.
    """
    latest = (
        db.query(TaskOwner)
        .filter(TaskOwner.session_id == session_id)
        .order_by(TaskOwner.updated_at.desc())
        .first()
    )
    if latest is None:
        return None
    task = db.get(Task, latest.task_id)
    return task.project if task is not None else None


def _task_broken_and_done(
    db, session_id: str, project_scope: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """VIỆC 3 groups B (việc hỏng) and C (việc xong).

    B persists until the owning session acts on the task again (its
    ``TaskEvent`` stays the newest one for that task). C is read-once: the
    session's ``SessionEventCursor`` is advanced past everything returned,
    so it never repeats (VIỆC 3, "đọc rồi thì biến mất").
    """
    broken: list[dict[str, Any]] = []
    hidden = 0
    try:
        latest_per_task = (
            db.query(TaskEvent.task_id, func.max(TaskEvent.id).label("max_id"))
            .filter(TaskEvent.event_type.in_(FAILED_EVENT_TYPES))
            .group_by(TaskEvent.task_id)
            .subquery()
        )
        broken_query = (
            db.query(TaskEvent)
            .join(latest_per_task, TaskEvent.id == latest_per_task.c.max_id)
            .join(Task, Task.id == TaskEvent.task_id)
            .filter(
                Task.archived_at.is_(None),
                Task.status.notin_(["done", "cancelled"]),
                _owner_visible_filter(db, session_id),
            )
        )
        if project_scope:
            broken_query = broken_query.filter(Task.project == project_scope)
        total_broken = broken_query.count()
        rows = list(broken_query.order_by(TaskEvent.id.desc()).limit(10))
        hidden += max(0, total_broken - len(rows))
        for event in rows:
            payload = event.payload or {}
            broken.append({
                "id": event.task_id,
                "kind": f"failed:{event.event_type}",
                "why": payload.get("why") or payload.get("error") or payload.get("reason") or "",
                "next": payload.get("next") or _NEXT_BY_EVENT_TYPE.get(event.event_type, "gọi get_status để xem chi tiết"),
                "waiting_since": event.created_at.isoformat() if event.created_at else None,
            })
    except Exception:
        broken = []

    done: list[dict[str, Any]] = []
    try:
        info_events = TaskEventService(db).get_digest(session_id, limit=50)
        relevant = [e for e in info_events if e.event_type in DONE_EVENT_TYPES]
        if project_scope and relevant:
            task_ids = {e.task_id for e in relevant}
            projects = {
                row.id: row.project
                for row in db.query(Task.id, Task.project).filter(Task.id.in_(task_ids))
            }
            relevant = [e for e in relevant if projects.get(e.task_id) == project_scope]
        for event in relevant[:10]:
            done.append({
                "id": event.task_id,
                "kind": f"done:{event.event_type}",
                "waiting_since": event.created_at.isoformat() if event.created_at else None,
            })
        # Advance the cursor past EVERY digested info event, not just the
        # DONE_EVENT_TYPES subset -- get_digest() does not move the cursor
        # itself, and a cursor that never moves would replay the same batch
        # forever, the opposite of "đọc rồi thì biến mất" (VIỆC 3).
        if info_events:
            TaskEventService(db).advance_cursor(
                session_id, max(e.id for e in info_events)
            )
    except Exception:
        done = []
    return broken, done[:10], hidden


def _ensure_session(db, claims: TokenClaims) -> str:
    """Give every native token a real router session, including executor tokens.

    Identity follows the CONNECTION, not the token: several terminals opened
    in the same repo dir share one ``.mcp.json`` and therefore one token, but
    each opens its own HTTP connection and gets its own ``Mcp-Session-Id``
    from the transport. Prefer that header so the server can tell them apart;
    fall back to the token's claims only when no HTTP request is active (the
    in-process transport used by tests).
    """
    header_session_id = _current_http_headers().get("mcp-session-id")
    session_id = header_session_id or claims.session_id or claims.token_id or "mcp"
    row = db.get(SessionModel, session_id)
    if row is not None:
        row.last_activity_at = _utcnow()
        db.commit()
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
        # Argument validation comes AFTER authn/authz and BEFORE any DB session.
        #
        # After: a caller with no right to this tool must be told "forbidden",
        # not handed a description of its parameters — argument feedback is a
        # small oracle and it belongs behind the permission check. Ordering it
        # the other way also changed the error a forbidden call receives, which
        # is what test_mcp_tool_list_is_filtered_by_token_role caught.
        #
        # Before: a malformed call should not cost a database connection, and
        # must never reach a handler that would quietly discard part of it.
        problems = validate_tool_arguments(spec, kwargs)
        if problems:
            return {
                "ok": False,
                "data": None,
                "error": describe_problems(
                    spec.name,
                    problems,
                    (spec.parameters.get("properties") or {}).keys(),
                ),
            }
        scoped_kwargs = _task_scope_arguments(claims, spec, kwargs)
        if not _task_scope_ok(claims, spec, scoped_kwargs):
            return {"ok": False, "data": None, "error": {"code": "task_scope_violation", "message": "Executor token is scoped to a different task"}}
        db = SessionLocal()
        try:
            session_id = _ensure_session(db, claims)
            if spec.name == "approve_gate":
                blocked = _verdict_evidence_block(db, scoped_kwargs)
                if blocked is not None:
                    return blocked
            result = await CommandRouter(db).execute_tool(spec.name, scoped_kwargs, session_id)
            _persist_verdict_evidence(db, spec.name, scoped_kwargs, result)
            _register_task_ownership(db, spec.name, scoped_kwargs, result, session_id)
            if (
                runtime_version is not None
                and spec.name in {
                    "get_status",
                    "land_task",
                    "dispatch_task",
                    "approve_gate",
                }
                and not result.get("error")
            ):
                warning = runtime_version.stale_warning(db=db)
                if warning is not None:
                    result = {**result, "runtime_warning": warning}
            # Native calls bypass the REST endpoint, so invalidate the same
            # context cache the old /api/mcp/tools/call path invalidated.
            invalidate_context_snapshot(db, project_id=None)
            response = envelope(result, next_step=_next_step(result))
            _attach_gate_brief(db, spec.name, result, response)
            project_scope = _session_project_scope(db, session_id)
            pending = _pending_approvals(db, session_id=session_id, project_scope=project_scope)
            if pending:
                response["pending_approvals"] = pending
                response["pending_approvals_note"] = _pending_approvals_note(pending)
            broken, done, inbox_hidden = _task_broken_and_done(db, session_id, project_scope)
            if broken:
                response["failed_work"] = broken
            if done:
                response["completed_work"] = done
            if inbox_hidden:
                response["failed_or_completed_hidden_count"] = inbox_hidden
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
