from __future__ import annotations

import json
import subprocess
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.mcp_native as mcp_native
from app.core.runtime_version import RuntimeVersionMonitor
from app.db.base import Base
from app.db.models import (
    Agent,
    InboxItem,
    Project,
    Session as SessionModel,
    Task,
    TaskEvent,
    TaskOwner,
)
from app.mcp_native import (
    _task_scope_arguments,
    _task_scope_ok,
    authenticate_token,
    build_server,
    envelope,
    issue_token,
)
from app.services.task_orchestration import TaskOrchestrationService
from app.services.tool_registry import get_mcp_tool_specs


def _runtime_monitor(tmp_path, *, advance_head: bool = True) -> RuntimeVersionMonitor:
    repo = tmp_path / "agenticmatix"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=True
        )
    (repo / "base.txt").write_text("base\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "base.txt"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "base"],
        capture_output=True,
        check=True,
    )
    monitor = RuntimeVersionMonitor.capture(repo)
    if advance_head:
        (repo / "landed.txt").write_text("landed\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "landed.txt"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "landed"],
            capture_output=True,
            check=True,
        )
    return monitor


def test_role_token_round_trip_and_task_scope_claim():
    token = issue_token("secret", role="executor", task_id="task-1")
    claims = authenticate_token(token, secret="secret")
    assert claims is not None
    assert claims.role == "executor"
    assert claims.task_id == "task-1"
    assert authenticate_token(token, secret="wrong") is None


def test_unsigned_or_legacy_token_is_rejected():
    assert authenticate_token("legacy", secret="secret") is None


def test_executor_task_scope_is_driven_by_tool_schema():
    claims = mcp_native.TokenClaims(role="executor", task_id="task-1")
    specs = {spec.name: spec for spec in get_mcp_tool_specs()}

    # Tools without a task_id field are scoped by the token/session and must
    # not require a fabricated argument.
    graph_args = _task_scope_arguments(claims, specs["get_impact_radius"], {"file": "a.py"})
    assert graph_args == {"file": "a.py"}
    assert _task_scope_ok(claims, specs["get_impact_radius"], graph_args)

    # An optional task_id is inferred from the executor token when omitted.
    status_args = _task_scope_arguments(claims, specs["get_status"], {})
    assert status_args == {"task_id": "task-1"}
    assert _task_scope_ok(claims, specs["get_status"], status_args)

    # Explicitly naming another task remains forbidden.
    assert not _task_scope_ok(
        claims, specs["get_status"], {"task_id": "task-2"}
    )

    # spec_get remains usable as a project-spec lookup when no task selector
    # is passed, but an executor may only name its own task explicitly.
    spec_args = _task_scope_arguments(claims, specs["spec_get"], {"ids": ["spec-1"]})
    assert spec_args == {"ids": ["spec-1"]}
    assert _task_scope_ok(claims, specs["spec_get"], spec_args)
    assert _task_scope_ok(claims, specs["spec_get"], {"task_id": "task-1"})
    assert not _task_scope_ok(claims, specs["spec_get"], {"task_id": "task-2"})


def test_native_envelope_structures_transition_error_and_hint():
    result = envelope({"error": "Task is already dispatched; expected status todo"})
    assert result["ok"] is False
    assert result["error"]["code"] == "task_transition_conflict"
    assert "hint" in result["error"]


def test_native_envelope_includes_next_for_task_state():
    router_result = {
        "action": "gate_decision",
        "decision": "rejected",
        "task": {"id": "task-1", "status": "awaiting-review"},
    }
    result = envelope(router_result, next_step=mcp_native._next_step(router_result))
    assert result["ok"] is True
    assert result["next"] == "Gọi request_review để bắt đầu review độc lập."


def test_pending_review_order_includes_the_persisted_approval_prompt(db_session):
    db_session.add(Project(id="pending-review-project", name="P", repo_root="/tmp"))
    db_session.add_all([
        Agent(id="@pending-executor", name="Executor", role="executor", cli="codex"),
        Agent(id="@pending-reviewer", name="Reviewer", role="reviewer", cli="codex"),
    ])
    db_session.add(
        Task(
            id="PENDING-REVIEW",
            title="Review reminder",
            project="pending-review-project",
            status="awaiting-review",
            mode="supervised",
            executor="@pending-executor",
            result_ref="base..head",
        )
    )
    db_session.commit()
    result = TaskOrchestrationService(db_session).request_review(
        task_id="PENDING-REVIEW",
        reviewer="@pending-reviewer",
        actor="system:test",
        idempotency_key="pending-review-gate",
        selection_reason="best capability match with 90% success_rate",
    )

    pending = mcp_native._pending_approvals(db_session)

    assert pending == [{
        "id": "PENDING-REVIEW",
        "kind": "task:review_order",
        "waiting_since": pending[0]["waiting_since"],
        # Who decides is derived from the task's mode rather than assumed --
        # see test_coordinator_authority.py (CTV2-1391).
        "decided_by": pending[0]["decided_by"],
        "prompt": result.gate_record.input_payload["approval_prompt"],
        # CTV2-1393: a short derived summary, and unknowns since this task
        # has no spec_task_link -- see test_gate_briefs.py for the full brief.
        "summary": pending[0]["summary"],
        "unknowns": pending[0]["unknowns"],
    }]
    assert pending[0]["decided_by"] in {"coordinator", "human"}
    assert "Reviewer đề xuất: @pending-reviewer" in pending[0]["prompt"]
    assert "best capability match" in pending[0]["prompt"]
    assert "@pending-reviewer" in pending[0]["summary"]
    assert pending[0]["unknowns"]


@pytest.mark.asyncio
async def test_tool_call_end_to_end_through_mcp_client(monkeypatch, tmp_path):
    """Full call path through a real fastmcp client. Regression for the
    handler signature bug: FunctionTool built from an explicit JSON schema
    never receives an injected Context, so a handler requiring one fails on
    every single call — which only surfaces when a tool is actually invoked
    through the protocol, not when the handler is unit-called directly."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="T-1", title="One", project="p1", status="todo"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="coordinator")
    monitor = _runtime_monitor(tmp_path, advance_head=False)
    server = build_server(default_token=token, runtime_version=monitor)
    async with Client(server) as client:
        result = await client.call_tool("get_status", {"task_id": "T-1"})
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    assert body["data"]["task"]["id"] == "T-1"
    assert "runtime_warning" not in body["data"]


@pytest.mark.asyncio
async def test_spec_task_link_round_trip_through_real_mcp_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="spec-mcp", name="Spec MCP"))
    seed.add(Task(id="SPEC-MCP-1", title="Link a spec", project="spec-mcp"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))

    async with Client(server) as client:
        write_result = await client.call_tool("spec_write", {"ops": [
            {
                "op": "create", "id": "spec-mcp-1", "project_id": "spec-mcp",
                "kind": "requirement", "title": "MCP linkage", "body": "Expose the edge",
            },
            {
                "op": "task_link", "spec_item_id": "spec-mcp-1", "task_id": "SPEC-MCP-1",
                "relation": "implements", "confidence": "asserted", "created_by": "@human",
            },
        ]})
        read_result = await client.call_tool("spec_get", {"task_id": "SPEC-MCP-1"})

    written = json.loads(write_result.content[0].text)
    fetched = json.loads(read_result.content[0].text)
    assert written["ok"] is True, written
    assert written["data"]["task_links"][0]["spec_item_id"] == "spec-mcp-1"
    assert fetched["ok"] is True, fetched
    assert [item["id"] for item in fetched["data"]["items"]] == ["spec-mcp-1"]
    assert fetched["data"]["task_links"][0]["task_id"] == "SPEC-MCP-1"


@pytest.mark.asyncio
async def test_realization_projection_round_trip_through_real_mcp_client(
    monkeypatch, git_repo_root
):
    """spec_get must expose the derived agreed/built projection, and
    spec_write must reject any attempt to set it, over the real MCP
    tool-call path (CTV2-1395)."""
    from pathlib import Path

    (Path(git_repo_root) / "mod.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=git_repo_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add mod"],
        cwd=git_repo_root, check=True, capture_output=True,
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="real-mcp", name="Real MCP", repo_root=git_repo_root))
    seed.add(Task(
        id="REAL-MCP-1", title="Implement it", project="real-mcp", status="done",
        executor="@executor", reviewer="@reviewer", result_ref="base..head",
    ))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))

    async with Client(server) as client:
        rejected = await client.call_tool("spec_write", {"ops": [{
            "op": "create", "id": "real-mcp-item", "project_id": "real-mcp",
            "kind": "design", "title": "x", "body": "x", "realization": "built",
        }]})
        written = await client.call_tool("spec_write", {"ops": [
            {
                "op": "create", "id": "real-mcp-built", "project_id": "real-mcp",
                "kind": "design", "title": "built", "body": "x", "status": "active",
            },
            {
                "op": "anchor", "spec_item_id": "real-mcp-built", "repo": "real-mcp",
                "path": "mod.py", "symbol": "foo", "relation": "implements",
            },
            {
                "op": "task_link", "spec_item_id": "real-mcp-built", "task_id": "REAL-MCP-1",
                "relation": "implements", "confidence": "asserted", "created_by": "@executor",
            },
            {
                "op": "create", "id": "real-mcp-agreed", "project_id": "real-mcp",
                "kind": "design", "title": "agreed", "body": "x", "status": "active",
            },
        ]})
        fetched = await client.call_tool(
            "spec_get", {"filter": {"project_id": "real-mcp", "backlog": True}}
        )

    rejected_body = json.loads(rejected.content[0].text)
    written_body = json.loads(written.content[0].text)
    fetched_body = json.loads(fetched.content[0].text)

    assert rejected_body["ok"] is False, rejected_body
    error = rejected_body["error"]
    error_text = error["message"] if isinstance(error, dict) else str(error)
    assert "realization" in error_text

    assert written_body["ok"] is True, written_body
    by_id = {item["id"]: item for item in written_body["data"]["items"]}
    assert by_id["real-mcp-built"]["realization"]["state"] == "built"
    assert by_id["real-mcp-agreed"]["realization"]["state"] == "agreed"

    assert fetched_body["ok"] is True, fetched_body
    assert [item["id"] for item in fetched_body["data"]["items"]] == ["real-mcp-agreed"]


@pytest.mark.asyncio
async def test_executor_can_call_graph_tools_without_task_id_argument(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    execute_tool = AsyncMock(return_value={"status": "success"})
    monkeypatch.setattr(mcp_native.CommandRouter, "execute_tool", execute_tool)

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        for name, args in (
            ("get_minimal_context", {"query": "dispatch"}),
            ("get_impact_radius", {"file": "app/main.py"}),
        ):
            result = await client.call_tool(name, args)
            body = json.loads(result.content[0].text)
            assert body["ok"] is True, body

    assert [call.args[1] for call in execute_tool.await_args_list] == [
        {"query": "dispatch"},
        {"file": "app/main.py"},
    ]


@pytest.mark.asyncio
async def test_executor_get_status_without_task_id_uses_token_scope(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool("get_status", {})
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    assert body["data"]["task"]["id"] == "task-1"


@pytest.mark.asyncio
async def test_executor_token_cannot_use_any_tool_path_to_make_own_task_done(
    monkeypatch,
):
    """CTV2-1363: exercise every client-facing completion path with the
    executor's own task-scoped token.  attach_result reaches the FSM and is
    rejected from in-review; verdict approval and landing remain coordinator
    only at the MCP boundary.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(
        id="task-1",
        title="Own result",
        project="p1",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base..head",
    ))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)

    attempts = (
        ("attach_result", {"task_id": "task-1", "commit": "new-base..new-head"}),
        ("land_task", {"task_id": "task-1"}),
        ("record_verdict", {"task_id": "task-1", "verdict": "pass"}),
        ("approve_gate", {"task_id": "task-1", "decision": "approved"}),
    )
    async with Client(server) as client:
        responses = {}
        for tool_name, arguments in attempts:
            result = await client.call_tool(tool_name, arguments)
            responses[tool_name] = json.loads(result.content[0].text)

    assert responses["attach_result"]["ok"] is False
    assert responses["attach_result"]["error"]["code"] == "task_transition_conflict"
    for tool_name in ("land_task", "record_verdict", "approve_gate"):
        assert responses[tool_name]["ok"] is False
        assert responses[tool_name]["error"]["code"] == "forbidden"

    verify = session_factory()
    task = verify.get(Task, "task-1")
    assert task.status == "in-review"
    assert task.final_verdict is None
    verify.close()


@pytest.mark.asyncio
async def test_land_task_result_warns_that_landed_code_needs_restart(
    monkeypatch, tmp_path
):
    monitor = _runtime_monitor(tmp_path)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        mcp_native.CommandRouter,
        "execute_tool",
        AsyncMock(
            return_value={
                "action": "landed",
                "task_id": "T-1",
                "status": "done",
                "landed_ref": "new-head",
            }
        ),
    )

    server = build_server(
        default_token=issue_token("test-secret", role="coordinator"),
        runtime_version=monitor,
    )
    async with Client(server) as client:
        result = await client.call_tool("land_task", {"task_id": "T-1"})
        body = json.loads(result.content[0].text)

    warning = body["data"]["runtime_warning"]
    assert warning["pending_commit_count"] == 1
    assert "Code đã vào main nhưng CHƯA có hiệu lực" in warning["message"]
    assert "restart backend/worker" in warning["message"]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "payload"),
    [
        (
            "dispatch_task",
            {"task_id": "T-1", "executor": "@executor"},
            {
                "action": "gate_created",
                "task_id": "T-1",
                "executor": "@executor",
                "gate_record_id": "gate-dispatch",
            },
        ),
        (
            "approve_gate",
            {"task_id": "T-1", "decision": "approved"},
            {
                "action": "gate_approved",
                "task_id": "T-1",
                "gate_record_id": "gate-approved",
                "nudged": True,
            },
        ),
    ],
)
@pytest.mark.parametrize(
    "head_changed", [False, True], ids=["matching-sha", "stale-sha"]
)
@pytest.mark.asyncio
async def test_risky_action_runtime_warning_matches_runtime_sha(
    monkeypatch,
    tmp_path,
    tool_name,
    arguments,
    payload,
    head_changed,
):
    monitor = _runtime_monitor(tmp_path, advance_head=head_changed)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(
        mcp_native.CommandRouter,
        "execute_tool",
        AsyncMock(return_value=payload),
    )

    server = build_server(
        default_token=issue_token("test-secret", role="coordinator"),
        runtime_version=monitor,
    )
    async with Client(server) as client:
        result = await client.call_tool(tool_name, arguments)
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    for field, value in payload.items():
        assert body["data"][field] == value
    if head_changed:
        warning = body["data"]["runtime_warning"]
        assert warning["code"] == "runtime_restart_required"
        assert warning["pending_commit_count"] == 1
    else:
        assert "runtime_warning" not in body["data"]


@pytest.mark.asyncio
async def test_mcp_tool_list_is_filtered_by_token_role(monkeypatch):
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    coordinator = build_server(
        default_token=issue_token("test-secret", role="coordinator")
    )
    executor = build_server(
        default_token=issue_token("test-secret", role="executor", task_id="task-1")
    )

    async with Client(coordinator) as coordinator_client:
        coordinator_tools = await coordinator_client.list_tools()
    async with Client(executor) as executor_client:
        executor_tools = await executor_client.list_tools()

    # Suy số lượng từ registry, KHÔNG hard-code: thêm tool mới là con số đổi.
    # Bản đầu hard-code 28/7 và vỡ ngay khi CTV2-1341 thêm spec_write/spec_get
    # (28 -> 30) dù logic lọc vẫn đúng.
    all_specs = get_mcp_tool_specs()
    expected_executor = {
        spec.name for spec in all_specs if spec.required_role == "executor"
    }

    assert len(coordinator_tools) == len(all_specs)
    assert len(executor_tools) == len(expected_executor)
    assert {tool.name for tool in executor_tools} == expected_executor
    assert {tool.name for tool in executor_tools} <= {
        tool.name for tool in coordinator_tools
    }
    assert "manage_agent" in {tool.name for tool in coordinator_tools}
    assert "manage_agent" not in {tool.name for tool in executor_tools}

    async with Client(executor) as executor_client:
        forbidden = await executor_client.call_tool("manage_agent", {})
    assert json.loads(forbidden.content[0].text)["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_unknown_arguments_are_rejected_instead_of_silently_dropped(monkeypatch):
    """A tool must never answer "created" to an argument it threw away.

    Four tasks were created with description=... on 2026-08-05, back when
    create_task did not declare that parameter: each returned
    {"action": "created"} and discarded the whole description. The loss surfaced
    days later, while investigating something unrelated.

    `description` is an accepted parameter now (see the test below), so this
    uses a name that is still genuinely unknown — the guard is about the class
    of mistake, not that one field.
    """
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))

    async with Client(server) as client:
        result = await client.call_tool(
            "create_task",
            {"title": "T", "project": "agenticmatix", "acceptance_criteria": ["x"]},
        )

    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "unknown_arguments"
    assert "acceptance_criteria" in error["message"]
    # The caller must learn the value is gone and how to store it properly.
    assert "update_task" in error["hint"]


@pytest.mark.asyncio
async def test_create_task_stores_the_description_it_accepts(monkeypatch):
    """Rejecting unknown names is only half the fix.

    Without a way to set the description at creation, every task would still
    start with nothing but a title for the planner to work from — and the
    fail-closed dispatch check would refuse it, which is how CTV2-1380 became
    unrecoverable. raw_input is the field the planner actually reads.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))
    # Multi-line with quotes and backticks: exactly what the old flag-string
    # encoding could not carry.
    spec_text = '# Problem\n\nMulti-line spec with `code` and "quotes".'

    async with Client(server) as client:
        result = await client.call_tool(
            "create_task",
            {"title": "T", "project": "p1", "description": spec_text},
        )

    payload = json.loads(result.content[0].text)
    assert payload.get("error") is None, payload
    task_id = payload["data"]["task_id"]

    check = session_factory()
    try:
        row = check.get(Task, task_id)
        assert row is not None, task_id
        assert row.raw_input == spec_text
        assert row.title == "T"
    finally:
        check.close()


@pytest.mark.asyncio
async def test_permission_is_checked_before_arguments(monkeypatch):
    """Argument feedback is an oracle; it belongs behind the permission check.

    A first draft validated arguments first, so an executor token calling a
    coordinator-only tool learned about its parameters instead of being refused.
    """
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    executor = build_server(
        default_token=issue_token("test-secret", role="executor", task_id="task-1")
    )

    async with Client(executor) as client:
        # Both forbidden AND malformed: the permission error must win.
        result = await client.call_tool("manage_agent", {"bogus_argument": 1})

    assert json.loads(result.content[0].text)["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_mcp_tool_list_filters_each_http_connection_by_its_token(monkeypatch):
    """One HTTP server must project a different tools/list for each token."""
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    app = mcp_native.build_http_app()

    def httpx_client_factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            **kwargs,
        )

    async with app.router.lifespan_context(app):
        tokens = [
            issue_token("test-secret", role="coordinator"),
            issue_token("test-secret", role="executor", task_id="task-1"),
        ]
        projections = []
        for token in tokens:
            transport = StreamableHttpTransport(
                "http://testserver/mcp",
                auth=token,
                httpx_client_factory=httpx_client_factory,
            )
            async with Client(transport) as client:
                projections.append(await client.list_tools())

    all_specs = get_mcp_tool_specs()
    executor_names = {
        spec.name for spec in all_specs if spec.required_role == "executor"
    }
    assert len(projections[0]) == len(all_specs)
    assert len(projections[1]) == len(executor_names)
    assert "manage_agent" in {tool.name for tool in projections[0]}
    assert "manage_agent" not in {tool.name for tool in projections[1]}


@pytest.mark.asyncio
async def test_tool_call_without_token_is_unauthorized(monkeypatch):
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server()  # no default token, no HTTP headers
    async with Client(server) as client:
        result = await client.call_tool("get_status", {"task_id": "T-1"})
        body = json.loads(result.content[0].text)
    assert body["ok"] is False
    assert body["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_save_project_context_executor_token_matching_task_id_succeeds(monkeypatch):
    """Regression for F1: the ToolSpec for save_project_context had no task_id
    parameter, so _task_scope_ok rejected every executor call with
    task_scope_violation before the handler ever ran."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "save_project_context",
            {
                "task_id": "task-1",
                "project_id": "p1",
                "context_md": "# Stack\nFastAPI",
                "rules": [],
            },
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is True, body
    assert body["data"]["status"] == "success"
    assert body["data"]["project_id"] == "p1"


@pytest.mark.asyncio
async def test_save_project_context_executor_token_mismatched_task_id_rejected(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="p1", name="P", repo_root="/tmp"))
    seed.add(Task(id="task-1", title="One", project="p1", status="dispatched"))
    seed.add(Task(id="task-2", title="Two", project="p1", status="dispatched"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")

    token = issue_token("test-secret", role="executor", task_id="task-1")
    server = build_server(default_token=token)
    async with Client(server) as client:
        result = await client.call_tool(
            "save_project_context",
            {
                "task_id": "task-2",
                "project_id": "p1",
                "context_md": "# Stack\nFastAPI",
                "rules": [],
            },
        )
        body = json.loads(result.content[0].text)

    assert body["ok"] is False, body
    assert body["error"]["code"] == "task_scope_violation"


@pytest.mark.asyncio
async def test_manage_inbox_crud_mapping_and_promote_end_to_end(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    seed = session_factory()
    seed.add(Project(id="ideas", name="Ideas", repo_root="/tmp", task_prefix="IDEA"))
    seed.commit()
    seed.close()

    monkeypatch.setattr(mcp_native, "SessionLocal", session_factory)
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    server = build_server(default_token=issue_token("test-secret", role="coordinator"))

    async with Client(server) as client:
        bad = await client.call_tool("manage_inbox", {"action": "add", "content": "x", "project_id": "missing"})
        assert "does not exist" in json.loads(bad.content[0].text)["error"]["message"]

        added = await client.call_tool("manage_inbox", {
            "action": "add", "content": "Build an inbox", "project_id": "ideas", "tags": ["product"]
        })
        add_body = json.loads(added.content[0].text)
        assert add_body["ok"] is True, add_body
        item_id = add_body["data"]["item"]["id"]

        updated = await client.call_tool("manage_inbox", {
            "action": "update", "id": item_id,
            "patch": {"content": "Build a better inbox", "tags": ["product", "v2"], "status": "open"},
        })
        assert json.loads(updated.content[0].text)["data"]["item"]["content"] == "Build a better inbox"

        listed = await client.call_tool("manage_inbox", {"action": "list", "q": "better", "status": "open"})
        assert json.loads(listed.content[0].text)["data"]["count"] == 1

        promoted = await client.call_tool("manage_inbox", {"action": "promote", "id": item_id, "title": "Inbox task"})
        promote_body = json.loads(promoted.content[0].text)
        assert promote_body["ok"] is True, promote_body
        assert promote_body["data"]["task_id"] == "IDEA-001"
        assert promote_body["data"]["item"]["status"] == "triaged"

        verify = session_factory()
        task = verify.get(Task, "IDEA-001")
        item = verify.get(InboxItem, item_id)
        assert task.raw_input == "Build a better inbox"
        assert item.task_id == task.id
        verify.close()

        second = await client.call_tool("manage_inbox", {"action": "add", "content": "Delete me"})
        second_id = json.loads(second.content[0].text)["data"]["item"]["id"]
        deleted = await client.call_tool("manage_inbox", {"action": "delete", "id": second_id})
        assert json.loads(deleted.content[0].text)["data"]["action"] == "deleted"


# --- CTV2-1399: đắc chủ + đẩy việc-hỏng/việc-xong -----------------------


@pytest.mark.asyncio
async def test_two_http_connections_get_distinct_session_ids(monkeypatch):
    """Two terminals sharing one token must still be told apart (VIỆC 1).

    ``_ensure_session`` must key off the transport's ``Mcp-Session-Id``
    header rather than the token's claims -- otherwise every terminal in the
    same repo dir (same ``.mcp.json``, same token) collapses onto one row.
    """
    monkeypatch.setattr(mcp_native.settings, "MCP_TOKEN_SECRET", "test-secret")
    app = mcp_native.build_http_app()

    def httpx_client_factory(**kwargs):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            **kwargs,
        )

    token = issue_token("test-secret", role="coordinator")
    session_ids: list[str] = []
    async with app.router.lifespan_context(app):
        for _ in range(2):
            transport = StreamableHttpTransport(
                "http://testserver/mcp",
                auth=token,
                httpx_client_factory=httpx_client_factory,
            )
            async with Client(transport) as client:
                await client.call_tool("get_status", {})
                session_ids.append(transport.get_session_id())

    assert session_ids[0] != session_ids[1]


def test_registering_tool_assigns_owner_and_last_writer_wins(db_session):
    """VIỆC 2: A creates, B approves the gate -> B owns it; A acts again ->
    A takes it back."""
    db_session.add(Project(id="own-proj", name="P", repo_root="/tmp"))
    db_session.add_all([
        Agent(id="@own-executor", name="Executor", role="executor", cli="codex"),
        Agent(id="@own-reviewer", name="Reviewer", role="reviewer", cli="codex"),
    ])
    db_session.add(Task(
        id="OWN-1", title="t", project="own-proj", status="in-review",
        mode="bypass", executor="@own-executor", reviewer="@own-reviewer",
        result_ref="base..head", current_gate="verdict",
    ))
    db_session.commit()

    result = {"task": {"id": "OWN-1", "status": "in-review"}}
    mcp_native._register_task_ownership(db_session, "create_task", {}, result, "session-A")
    owner = db_session.get(TaskOwner, "OWN-1")
    assert owner.session_id == "session-A"

    mcp_native._register_task_ownership(
        db_session, "approve_gate", {"gate_id": "OWN-1"}, result, "session-B"
    )
    owner = db_session.get(TaskOwner, "OWN-1")
    assert owner.session_id == "session-B"

    mcp_native._register_task_ownership(db_session, "update_task", {"task_id": "OWN-1"}, result, "session-A")
    owner = db_session.get(TaskOwner, "OWN-1")
    assert owner.session_id == "session-A"


def test_read_only_tools_never_register_ownership(db_session):
    db_session.add(Project(id="ro-proj", name="P", repo_root="/tmp"))
    db_session.add(Task(id="RO-1", title="t", project="ro-proj", status="todo"))
    db_session.commit()

    result = {"task": {"id": "RO-1", "status": "todo"}}
    for tool_name in ("get_status", "query_db", "get_run_output", "get_task_events", "wait_for_task", "spec_get"):
        mcp_native._register_task_ownership(db_session, tool_name, {"task_id": "RO-1"}, result, "session-X")
        assert db_session.get(TaskOwner, "RO-1") is None


def test_stale_owner_task_reappears_for_everyone(db_session):
    """VIỆC 2: an owner whose session has gone quiet no longer hides the
    task -- a dead connection must never make work vanish."""
    db_session.add(Project(id="stale-proj", name="P", repo_root="/tmp"))
    db_session.add(Agent(id="@stale-exec", name="E", role="executor", cli="codex"))
    db_session.add(Task(
        id="STALE-1", title="t", project="stale-proj", status="todo",
        mode="supervised", current_gate="dispatch", legacy_no_ac=True,
    ))
    db_session.commit()
    from datetime import datetime, timedelta, timezone

    stale_owner = SessionModel(
        id="owner-session", thread_id="owner-session", title="MCP coordinator",
        context_level="global", messages=[], status="active",
        last_activity_at=datetime.now(timezone.utc) - timedelta(seconds=mcp_native.OWNER_STALE_SECONDS + 60),
    )
    other_session = SessionModel(
        id="other-session", thread_id="other-session", title="MCP coordinator",
        context_level="global", messages=[], status="active",
    )
    db_session.add_all([stale_owner, other_session])
    db_session.add(TaskOwner(task_id="STALE-1", session_id="owner-session"))
    db_session.commit()

    # A gate that would otherwise be filtered out by ownership...
    from app.services.task_orchestration import TaskOrchestrationService
    TaskOrchestrationService(db_session).request_dispatch(
        task_id="STALE-1", agent_id="@stale-exec", actor="system:test",
        idempotency_key="stale-dispatch-1",
    )

    visible = mcp_native._pending_approvals(db_session, session_id="other-session")
    assert any(entry.get("id") == "STALE-1" for entry in visible)


def test_active_other_owner_hides_gate_from_third_session(db_session):
    db_session.add(Project(id="active-proj", name="P", repo_root="/tmp"))
    db_session.add(Agent(id="@active-exec", name="E", role="executor", cli="codex"))
    db_session.add(Task(
        id="ACTIVE-1", title="t", project="active-proj", status="todo",
        mode="supervised", current_gate="dispatch", legacy_no_ac=True,
    ))
    db_session.add(SessionModel(
        id="active-owner", thread_id="active-owner", title="MCP", context_level="global",
        messages=[], status="active",
    ))
    db_session.commit()
    db_session.add(TaskOwner(task_id="ACTIVE-1", session_id="active-owner"))
    db_session.commit()

    from app.services.task_orchestration import TaskOrchestrationService
    TaskOrchestrationService(db_session).request_dispatch(
        task_id="ACTIVE-1", agent_id="@active-exec", actor="system:test",
        idempotency_key="active-dispatch-1",
    )

    hidden_from_third = mcp_native._pending_approvals(db_session, session_id="third-session")
    assert not any(entry.get("id") == "ACTIVE-1" for entry in hidden_from_third)

    visible_to_owner = mcp_native._pending_approvals(db_session, session_id="active-owner")
    assert any(entry.get("id") == "ACTIVE-1" for entry in visible_to_owner)


def test_project_scope_filters_pending_approvals(db_session):
    """VIỆC 4: a coordinator scoped to project A never sees project B."""
    db_session.add_all([
        Project(id="proj-a", name="A", repo_root="/tmp"),
        Project(id="proj-b", name="B", repo_root="/tmp"),
        Agent(id="@scope-exec", name="E", role="executor", cli="codex"),
    ])
    db_session.add_all([
        Task(id="A-1", title="t", project="proj-a", status="todo", mode="supervised", current_gate="dispatch", legacy_no_ac=True),
        Task(id="B-1", title="t", project="proj-b", status="todo", mode="supervised", current_gate="dispatch", legacy_no_ac=True),
    ])
    db_session.commit()

    from app.services.task_orchestration import TaskOrchestrationService
    svc = TaskOrchestrationService(db_session)
    svc.request_dispatch(task_id="A-1", agent_id="@scope-exec", actor="system:test", idempotency_key="scope-a")
    svc.request_dispatch(task_id="B-1", agent_id="@scope-exec", actor="system:test", idempotency_key="scope-b")

    scoped = mcp_native._pending_approvals(db_session, project_scope="proj-a")
    ids = {entry.get("id") for entry in scoped}
    assert "A-1" in ids
    assert "B-1" not in ids


def test_failed_events_persist_until_task_leaves_failed_state(db_session):
    """VIỆC 3 group B: run_failed/landing_failed carry why+next and stay
    until acted on."""
    db_session.add(Project(id="fail-proj", name="P", repo_root="/tmp"))
    db_session.add(Task(id="FAIL-1", title="t", project="fail-proj", status="failed"))
    db_session.commit()
    from app.services.task_event_service import emit_task_event
    emit_task_event(
        task_id="FAIL-1", event_type="run_failed",
        payload={"error": "boom", "next": "sửa rồi dispatch lại"}, db=db_session,
        kind="decision",
    )

    broken, done, hidden = mcp_native._task_broken_and_done(db_session, "some-session", None)
    assert len(broken) == 1
    assert broken[0]["id"] == "FAIL-1"
    assert broken[0]["kind"] == "failed:run_failed"
    assert broken[0]["why"] == "boom"
    assert broken[0]["next"] == "sửa rồi dispatch lại"
    assert done == []
    assert hidden == 0


def test_done_events_are_read_once_then_vanish(db_session):
    """VIỆC 3 group C: read once, then gone -- unlike group A/B."""
    db_session.add(Project(id="done-proj", name="P", repo_root="/tmp"))
    db_session.add(Task(id="DONE-1", title="t", project="done-proj", status="todo"))
    db_session.commit()
    from app.services.task_event_service import emit_task_event
    emit_task_event(task_id="DONE-1", event_type="landed", payload={}, db=db_session)

    _, done_first, _ = mcp_native._task_broken_and_done(db_session, "reader-session", None)
    assert any(e["id"] == "DONE-1" for e in done_first)

    _, done_second, _ = mcp_native._task_broken_and_done(db_session, "reader-session", None)
    assert done_second == []


def test_no_new_events_adds_no_noise(db_session):
    """VIỆC 3: nothing new -> nothing added."""
    db_session.add(Project(id="quiet-proj", name="P", repo_root="/tmp"))
    db_session.commit()
    broken, done, hidden = mcp_native._task_broken_and_done(db_session, "quiet-session", None)
    assert broken == []
    assert done == []
    assert hidden == 0


def test_truncated_pending_approvals_reports_hidden_count(db_session):
    """VIỆC 4: cutting the list must say how much is hidden, not go quiet."""
    db_session.add(Project(id="many-proj", name="P", repo_root="/tmp"))
    db_session.add(Agent(id="@many-exec", name="E", role="executor", cli="codex"))
    db_session.add_all([
        Task(id=f"MANY-{i}", title="t", project="many-proj", status="todo", mode="supervised", current_gate="dispatch", legacy_no_ac=True)
        for i in range(7)
    ])
    db_session.commit()
    from app.services.task_orchestration import TaskOrchestrationService
    svc = TaskOrchestrationService(db_session)
    for i in range(7):
        svc.request_dispatch(
            task_id=f"MANY-{i}", agent_id="@many-exec", actor="system:test",
            idempotency_key=f"many-{i}",
        )

    pending = mcp_native._pending_approvals(db_session)
    hidden_entries = [e for e in pending if e.get("kind") == "meta:hidden"]
    assert hidden_entries and hidden_entries[0]["hidden_count"] >= 2
    note = mcp_native._pending_approvals_note(pending)
    assert "bị ẩn" in note
