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
from app.db.models import Agent, InboxItem, Project, Task
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
    result = envelope(
        {"task": {"id": "task-1", "status": "awaiting-review"}},
        next_step="Gọi request_review để bắt đầu review độc lập.",
    )
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
        "prompt": result.gate_record.input_payload["approval_prompt"],
    }]
    assert "Reviewer đề xuất: @pending-reviewer" in pending[0]["prompt"]
    assert "best capability match" in pending[0]["prompt"]


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
