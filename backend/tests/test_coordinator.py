from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from app.db.models import Agent, LLMUsage, Session, Task
from app.services.coordinator import (
    CoordinatorService,
)
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse
from app.services.task_orchestration import TaskOrchestrationService


@dataclass
class _FakeProvider:
    name: str
    replies: list[str]
    failures: list[Exception] = field(default_factory=list)
    calls: list[tuple[str, list[dict]]] = field(default_factory=list)

    async def complete(
        self,
        messages,
        model,
        stream=False,
        *,
        max_tokens=2048,
        temperature=0.7,
        tools=None,
    ):
        self.calls.append((model, messages))
        if self.failures:
            raise self.failures.pop(0)
        text = self.replies.pop(0)
        response = ProviderResponse(
            provider=self.name,
            model=model,
            text=text if not stream else "",
            usage=UsageCounts(input_tokens=100, output_tokens=20, cached_tokens=10),
            request_id=f"{self.name}-request",
            stop_reason="stop",
        )
        if stream:
            async def chunks():
                midpoint = max(1, len(text) // 2)
                for chunk in (text[:midpoint], text[midpoint:]):
                    if chunk:
                        yield chunk
                response.text = text

            response.chunks = chunks()
        return response


@dataclass
class _ScriptedToolProvider:
    """Fake provider that plays back one scripted response per ``complete``
    call and records the ``tools`` kwarg it was invoked with, so tests can
    assert on the active tool set at each tool-loop iteration."""

    name: str
    script: list[dict]
    calls: list[dict] = field(default_factory=list)

    async def complete(
        self,
        messages,
        model,
        stream=False,
        *,
        max_tokens=2048,
        temperature=0.7,
        tools=None,
    ):
        # `tools` is the coordinator's mutable active-set list; snapshot it
        # so later merges (append-in-place) don't retroactively change what
        # earlier recorded calls appear to have seen.
        self.calls.append({"tools": list(tools) if tools is not None else None, "messages": messages})
        step = self.script.pop(0)
        return ProviderResponse(
            provider=self.name,
            model=model,
            text=step.get("text", ""),
            usage=UsageCounts(input_tokens=10, output_tokens=5, cached_tokens=0),
            request_id=f"{self.name}-{len(self.calls)}",
            stop_reason="tool_calls" if step.get("tool_calls") else "stop",
            tool_calls=step.get("tool_calls"),
        )


@dataclass
class _FakeCLIDispatcher:
    """Minimal CLI dispatcher double for exercising the CLI-routed path."""

    replies: dict[str, str]
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def spawn(self, cli, model, prompt):
        self.calls.append((cli, model, prompt))
        yield self.replies[model]


def _service(db_session, openai, **kwargs):
    return CoordinatorService(
        db_session,
        providers={"openai": openai},
        retry_base_seconds=0,
        **kwargs,
    )


def _pending_dispatch_gate(db_session, task_id: str):
    db_session.add(
        Agent(
            id=f"@agent-{task_id.lower()}",
            name=f"Agent {task_id}",
            role="executor",
            cli="codex",
        )
    )
    db_session.add(
        Task(
            id=task_id,
            project=f"missing-project-{task_id.lower()}",
            title=f"Task {task_id}",
            status="todo",
            mode="supervised",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    with patch(
        "app.services.task_orchestration.build_dispatch_command",
        return_value=("codex exec task", "/tmp", "codex"),
    ):
        return TaskOrchestrationService(db_session).request_dispatch(
            task_id=task_id,
            agent_id=f"@agent-{task_id.lower()}",
            actor="@operator",
            idempotency_key=f"dispatch-{task_id.lower()}",
        ).gate_record


@pytest.mark.asyncio
async def test_openai_and_cli_paths_share_rehydrated_history_in_one_session(db_session):
    openai = _FakeProvider("openai", ["My name is Ada."])
    dispatcher = _FakeCLIDispatcher({"claude-sonnet-4": "Your name is Ada."})
    service = CoordinatorService(
        db_session,
        providers={"openai": openai},
        dispatcher=dispatcher,
        retry_base_seconds=0,
    )
    session = Session(id="session-switch", thread_id="session-switch", messages=[])
    db_session.add(session)
    db_session.commit()

    first = await service.complete_turn(
        session,
        "Remember that my name is Ada.",
        model="gpt-4o",
        idempotency_key="turn-1",
    )
    second = await service.complete_turn(
        session,
        "What is my name?",
        model="claude-sonnet-4",
        idempotency_key="turn-2",
    )

    assert first.provider == "openai"
    assert second.provider == "anthropic"
    cli, model, prompt = dispatcher.calls[0]
    assert cli == "claude"
    assert model == "claude-sonnet-4"
    assert "USER:\nRemember that my name is Ada." in prompt
    assert "ASSISTANT:\nMy name is Ada." in prompt
    assert "USER:\nWhat is my name?" in prompt
    assert session.selected_provider == "anthropic"
    assert session.selected_model == "claude-sonnet-4"
    assert db_session.query(LLMUsage).count() == 2


@pytest.mark.asyncio
async def test_idempotency_returns_persisted_turn_without_second_provider_call(db_session):
    openai = _FakeProvider("openai", ["Only once"])
    service = _service(db_session, openai)
    session = Session(id="session-idempotent", messages=[])
    db_session.add(session)
    db_session.commit()

    first = await service.complete_turn(
        session,
        "Hello",
        model="gpt-4o",
        idempotency_key="stable-turn",
    )
    second = await service.complete_turn(
        session,
        "Hello",
        model="gpt-4o",
        idempotency_key="stable-turn",
    )

    assert first.content == second.content == "Only once"
    assert second.cached is True
    assert len(openai.calls) == 1
    assert len(session.messages) == 2
    assert db_session.query(LLMUsage).count() == 1
    with pytest.raises(ValueError, match="different message"):
        await service.complete_turn(
            session,
            "Different content",
            model="gpt-4o",
            idempotency_key="stable-turn",
        )


@pytest.mark.asyncio
async def test_streaming_is_normalized_and_persisted_as_one_message(db_session):
    openai = _FakeProvider("openai", ["streamed reply"])
    service = _service(db_session, openai)
    session = Session(id="session-stream", messages=[])
    db_session.add(session)
    db_session.commit()

    chunks = [
        chunk
        async for chunk in service.stream_turn(
            session,
            "Stream this",
            model="gpt-4o",
            idempotency_key="stream-turn",
        )
    ]

    assert "".join(chunks) == "streamed reply"
    assistant = [m for m in session.messages if m["role"] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0]["content"] == "streamed reply"
    assert assistant[0]["status"] == "complete"


@pytest.mark.asyncio
async def test_transient_failure_retries_without_duplicate_user_message(db_session):
    openai = _FakeProvider(
        "openai",
        ["recovered"],
        failures=[TimeoutError("temporary")],
    )
    service = _service(db_session, openai, max_retries=1)
    session = Session(id="session-retry", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Retry me",
        model="gpt-4o",
        idempotency_key="retry-turn",
    )

    assert result.content == "recovered"
    assert len(openai.calls) == 2
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_loaded_tool_group_persists_across_turns(db_session):
    gate_record = _pending_dispatch_gate(db_session, "PERSIST-1")
    script_turn1 = [
        {"tool_calls": [{"id": "c1", "name": "load_tools", "input": {"group": "task_lifecycle"}}]},
        {"text": "Tools loaded."},
    ]
    script_turn2 = [
        {
            "tool_calls": [
                {
                    "id": "c2",
                    "name": "approve_gate",
                    "input": {"gate_record_id": gate_record.id},
                }
            ]
        },
        {"text": "Approved."},
    ]
    provider = _ScriptedToolProvider("openai", script_turn1 + script_turn2)
    service = _service(db_session, provider)
    session = Session(id="session-load-tools", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "load task lifecycle tools",
        model="gpt-4o",
        idempotency_key="turn-1",
    )
    assert result.content == "Tools loaded."

    baseline_names = {t["name"] for t in provider.calls[0]["tools"]}
    assert baseline_names == {"create_task", "get_status", "query_db", "load_tools"}

    expanded_names = {t["name"] for t in provider.calls[1]["tools"]}
    assert "approve_gate" in expanded_names
    assert expanded_names >= baseline_names
    assert session.state_payload["loaded_tool_groups"] == ["task_lifecycle"]

    with patch("app.workers.agent_runner.run_agent.send"):
        result = await service.complete_turn(
            session,
            "approve the pending gate",
            model="gpt-4o",
            idempotency_key="turn-2",
        )
    assert result.content == "Approved."

    turn2_names = {t["name"] for t in provider.calls[2]["tools"]}
    assert turn2_names == baseline_names | {
        "dispatch_task", "record_verdict", "approve_gate", "cancel_task",
        "request_review", "generate_spec_plan", "update_task",
    }
    tool_messages = [m for m in session.messages if m.get("name") == "approve_gate"]
    assert len(tool_messages) == 1
    approval = json.loads(tool_messages[0]["content"])
    assert approval["action"] == "gate_decision"
    assert approval["decision"] == "approved"
    assert "not loaded" not in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_deferred_tool_without_load_tools_auto_loads_and_executes(db_session):
    gate_record = _pending_dispatch_gate(db_session, "AUTOLOAD-1")
    script = [
        {
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "approve_gate",
                    "input": {"gate_record_id": gate_record.id},
                }
            ]
        },
        {"text": "Approved in one iteration."},
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider)
    session = Session(id="session-blocked", messages=[])
    db_session.add(session)
    db_session.commit()

    with patch("app.workers.agent_runner.run_agent.send"):
        result = await service.complete_turn(
            session,
            "approve the pending gate",
            model="gpt-4o",
            idempotency_key="turn-1",
        )

    assert result.content == "Approved in one iteration."
    tool_messages = [m for m in session.messages if m.get("name") == "approve_gate"]
    assert len(tool_messages) == 1
    approval = json.loads(tool_messages[0]["content"])
    assert approval["action"] == "gate_decision"
    assert approval["decision"] == "approved"
    assert "not loaded" not in tool_messages[0]["content"]
    assert session.state_payload["loaded_tool_groups"] == ["task_lifecycle"]


@pytest.mark.asyncio
async def test_stream_turn_loaded_tools_persist_across_turns(db_session):
    script_turn1 = [
        {"tool_calls": [{"id": "c1", "name": "load_tools", "input": {"group": "task_lifecycle"}}]},
        {"tool_calls": [{"id": "c2", "name": "dispatch_task", "input": {"task_id": "NOPE-1"}}]},
        {"text": "Done."},
    ]
    script_turn2 = [{"text": "Second turn, baseline only."}]
    provider = _ScriptedToolProvider("openai", script_turn1 + script_turn2)
    service = _service(db_session, provider)
    session = Session(id="session-stream-load-tools", messages=[])
    db_session.add(session)
    db_session.commit()

    events = [
        event
        async for event in service.stream_turn(
            session,
            "please dispatch NOPE-1",
            model="gpt-4o",
            idempotency_key="turn-1",
        )
    ]
    assert "".join(e for e in events if isinstance(e, str)) == "Done."

    baseline_names = {t["name"] for t in provider.calls[0]["tools"]}
    assert baseline_names == {"create_task", "get_status", "query_db", "load_tools"}

    expanded_names = {t["name"] for t in provider.calls[1]["tools"]}
    assert "dispatch_task" in expanded_names
    assert expanded_names >= baseline_names

    async for _ in service.stream_turn(
        session,
        "second turn",
        model="gpt-4o",
        idempotency_key="turn-2",
    ):
        pass
    turn2_names = {t["name"] for t in provider.calls[-1]["tools"]}
    assert turn2_names == baseline_names | {
        "dispatch_task", "record_verdict", "approve_gate", "cancel_task",
        "request_review", "generate_spec_plan", "update_task",
    }


def test_context_budget_keeps_newest_turns_and_system_prefix(db_session):
    service = CoordinatorService(
        db_session,
        max_output_tokens=10,
        context_safety_tokens=0,
        context_windows={"claude": 35},
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old " * 30},
        {"role": "assistant", "content": "older " * 20},
        {"role": "user", "content": "newest"},
    ]

    budgeted = service.budget_messages(messages, "claude-test")

    assert budgeted[0]["role"] == "system"
    assert budgeted[-1]["content"] == "newest"
    assert all(message["content"] != "old " * 30 for message in budgeted)


def test_context_budget_does_not_reorphan_tool_call_pairs(db_session):
    """budget_messages can drop the assistant side of a pair while keeping the
    (smaller, newer) tool result, which would otherwise leave a provider-invalid
    orphan tool message in the final request."""
    service = CoordinatorService(
        db_session,
        max_output_tokens=10,
        context_safety_tokens=0,
        context_windows={"claude": 35},
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant", "content": "older " * 20,
            "tool_calls": [{"id": "c1", "name": "get_status", "input": {}}],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "get_status", "content": "ok"},
        {"role": "user", "content": "newest"},
    ]

    budgeted = service.budget_messages(messages, "claude-test")

    assistant_ids = {
        call["id"]
        for message in budgeted
        if message["role"] == "assistant"
        for call in (message.get("tool_calls") or [])
    }
    tool_ids = {
        message["tool_call_id"]
        for message in budgeted
        if message["role"] == "tool"
    }
    assert assistant_ids == tool_ids
    assert budgeted[-1]["content"] == "newest"


def test_chat_endpoint_routes_requested_models_and_preserves_history(
    client,
    db_session,
    monkeypatch,
):
    openai = _FakeProvider("openai", ["first answer", "second answer"])
    monkeypatch.setattr(
        "app.api.chat.CoordinatorService",
        lambda db: CoordinatorService(db, providers={"openai": openai}),
    )

    first = client.post(
        "/api/chat",
        json={
            "thread_id": "api-switch-session",
            "message": "First question",
            "model": "gpt-4o",
            "idempotency_key": "api-turn-1",
        },
    )
    second = client.post(
        "/api/chat",
        json={
            "thread_id": "api-switch-session",
            "message": "Second question",
            "model": "gpt-4o-mini",
            "idempotency_key": "api-turn-2",
        },
    )

    assert first.status_code == second.status_code == 200
    assert '"type": "done"' in first.text
    assert '"type": "done"' in second.text
    assert openai.calls[0][1][0]["role"] == "system"
    conversation = [
        m for m in openai.calls[1][1] if m["role"] in {"user", "assistant"}
    ]
    assert [message["content"] for message in conversation] == [
        "First question",
        "first answer",
        "Second question",
    ]
    session = (
        db_session.query(Session)
        .filter(Session.thread_id == "api-switch-session")
        .one()
    )
    assert session.selected_provider == "openai"


@pytest.mark.asyncio
async def test_six_consecutive_tool_calls_complete_without_exception(db_session):
    """AC: Chuỗi 6 tool call liên tiếp hoàn thành, không exception."""
    script = [
        {"tool_calls": [{"id": "c1", "name": "load_tools", "input": {"group": "task_lifecycle"}}]},
        {"tool_calls": [{"id": "c2", "name": "get_status", "input": {}}]},
        {"tool_calls": [{"id": "c3", "name": "create_task", "input": {"project": "p1", "title": "t1"}}]},
        {"tool_calls": [{"id": "c4", "name": "get_status", "input": {}}]},
        {"tool_calls": [{"id": "c5", "name": "load_tools", "input": {"group": "system_admin"}}]},
        {"tool_calls": [{"id": "c6", "name": "query_db", "input": {"query": "SELECT 1"}}]},
        {"text": "Successfully ran all 6 tools."},
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider, max_tool_iterations=20)
    session = Session(id="session-6-tools", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Run sequence of 6 tools",
        model="gpt-4o",
        idempotency_key="turn-6-tools",
    )

    assert result.content == "Successfully ran all 6 tools."
    assistant_msg = [m for m in session.messages if m["role"] == "assistant" and "tool_iterations" in m][-1]
    assert assistant_msg["tool_iterations"] == 7


@pytest.mark.asyncio
async def test_repeated_tool_call_stops_early_with_message(db_session):
    """AC: Gọi trùng tool cùng args 3 lần → dừng sớm với thông báo."""
    script = [
        {"tool_calls": [{"id": "c1", "name": "get_status", "input": {"task_id": "T-1"}}]},
        {"tool_calls": [{"id": "c2", "name": "get_status", "input": {"task_id": "T-1"}}]},
        {"tool_calls": [{"id": "c3", "name": "get_status", "input": {"task_id": "T-1"}}]},
        {"text": "Should not reach here."},
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider, max_repeated_tool_calls=3)
    session = Session(id="session-repeated-tools", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Keep checking status",
        model="gpt-4o",
        idempotency_key="turn-repeated-tools",
    )

    assert "Turn stopped early" in result.content
    assert "detected repeated call to tool 'get_status'" in result.content
    assert "3 times in a row" in result.content
    assistant_msg = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert assistant_msg["status"] == "complete"
    assert assistant_msg["stop_reason"] == "repeated_tool_call"


@pytest.mark.asyncio
async def test_soft_stop_on_max_tool_iterations_exceeded(db_session):
    """AC: Chạm trần iterations → dừng mềm thay vì RuntimeError."""
    script = [
        {"tool_calls": [{"id": f"c{i}", "name": "get_status", "input": {"step": i}}]}
        for i in range(1, 10)
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider, max_tool_iterations=3)
    session = Session(id="session-max-iter", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Run forever",
        model="gpt-4o",
        idempotency_key="turn-max-iter",
    )

    assert "Turn reached maximum tool iteration limit (3 iterations)" in result.content
    assistant_msg = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert assistant_msg["status"] == "complete"
    assert assistant_msg["stop_reason"] == "max_iterations_exceeded"
    assert assistant_msg["tool_iterations"] == 3


@pytest.mark.asyncio
async def test_soft_stop_on_token_budget_exceeded(db_session):
    """AC: Có chặn chi phí theo token đã tiêu trong turn."""
    script = [
        {"tool_calls": [{"id": "c1", "name": "get_status", "input": {"step": 1}}]},
        {"tool_calls": [{"id": "c2", "name": "get_status", "input": {"step": 2}}]},
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider, max_turn_tokens=20)
    session = Session(id="session-token-budget", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Heavy turn",
        model="gpt-4o",
        idempotency_key="turn-token-budget",
    )

    assert "Turn reached maximum token budget" in result.content
    assistant_msg = [m for m in session.messages if m["role"] == "assistant"][-1]
    assert assistant_msg["status"] == "complete"
    assert assistant_msg["stop_reason"] == "token_budget_exceeded"


@pytest.mark.asyncio
async def test_duplicate_tool_call_returns_error_without_executing(db_session):
    """AC: Gọi tool lần 2 với cùng args → trả DUPLICATE_CALL error, không execute."""
    script = [
        {"tool_calls": [{"id": "c1", "name": "get_status", "input": {"task_id": "T-1"}}]},
        {"tool_calls": [{"id": "c2", "name": "get_status", "input": {"task_id": "T-1"}}]},
        {"tool_calls": [{"id": "c3", "name": "get_status", "input": {"task_id": "T-2"}}]},
        {"text": "Done checking."},
    ]
    provider = _ScriptedToolProvider("openai", script)
    service = _service(db_session, provider, max_repeated_tool_calls=3)
    session = Session(id="session-duplicate-detection", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Check status",
        model="gpt-4o",
        idempotency_key="turn-duplicate-detection",
    )

    assert result.content == "Done checking."

    tool_results = [
        m for m in session.messages
        if m["role"] == "tool"
    ]
    assert len(tool_results) == 3

    import json
    first_result = json.loads(tool_results[0]["content"])
    assert "DUPLICATE_CALL" not in str(first_result)

    second_result = json.loads(tool_results[1]["content"])
    assert second_result.get("error") == "DUPLICATE_CALL"
    assert "identical arguments" in second_result.get("message", "")

    third_result = json.loads(tool_results[2]["content"])
    assert "DUPLICATE_CALL" not in str(third_result)
