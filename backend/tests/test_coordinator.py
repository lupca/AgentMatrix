from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.db.models import LLMUsage, Session
from app.services.coordinator import (
    DEFAULT_PROVIDER_ROUTER,
    CoordinatorService,
    ProviderRouter,
)
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse


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


def test_provider_router_resolves_only_openai_adapter():
    openai = _FakeProvider("openai", [])
    router = ProviderRouter({"openai": openai})

    assert router.get("gpt-4o", "openai") is openai
    with pytest.raises(ValueError, match="Cannot infer provider"):
        router.get("unknown-model")
    with pytest.raises(ValueError, match="Unsupported coordinator provider"):
        router.get("claude-sonnet-4", "anthropic")


def test_chat_endpoint_routes_requested_models_and_preserves_history(
    client,
    db_session,
    monkeypatch,
):
    openai = _FakeProvider("openai", ["first answer", "second answer"])
    monkeypatch.setitem(
        DEFAULT_PROVIDER_ROUTER.providers,
        "openai",
        openai,
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
