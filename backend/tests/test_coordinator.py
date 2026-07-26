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


def _service(db_session, anthropic, google, **kwargs):
    return CoordinatorService(
        db_session,
        providers={"anthropic": anthropic, "google": google},
        retry_base_seconds=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_model_switch_rehydrates_canonical_history_and_records_usage(db_session):
    anthropic = _FakeProvider("anthropic", ["My name is Ada."])
    google = _FakeProvider("google", ["Your name is Ada."])
    service = _service(db_session, anthropic, google)
    session = Session(id="session-switch", thread_id="session-switch", messages=[])
    db_session.add(session)
    db_session.commit()

    first = await service.complete_turn(
        session,
        "Remember that my name is Ada.",
        model="claude-sonnet-4",
        idempotency_key="turn-1",
    )
    second = await service.complete_turn(
        session,
        "What is my name?",
        model="gemini-2.5-flash",
        idempotency_key="turn-2",
    )

    assert first.provider == "anthropic"
    assert second.provider == "google"
    google_history = google.calls[0][1]
    assert [message["content"] for message in google_history] == [
        "Remember that my name is Ada.",
        "My name is Ada.",
        "What is my name?",
    ]
    assert session.selected_provider == "google"
    assert session.selected_model == "gemini-2.5-flash"
    assert db_session.query(LLMUsage).count() == 2


@pytest.mark.asyncio
async def test_idempotency_returns_persisted_turn_without_second_provider_call(db_session):
    anthropic = _FakeProvider("anthropic", ["Only once"])
    google = _FakeProvider("google", [])
    service = _service(db_session, anthropic, google)
    session = Session(id="session-idempotent", messages=[])
    db_session.add(session)
    db_session.commit()

    first = await service.complete_turn(
        session,
        "Hello",
        model="claude-sonnet-4",
        idempotency_key="stable-turn",
    )
    second = await service.complete_turn(
        session,
        "Hello",
        model="claude-sonnet-4",
        idempotency_key="stable-turn",
    )

    assert first.content == second.content == "Only once"
    assert second.cached is True
    assert len(anthropic.calls) == 1
    assert len(session.messages) == 2
    assert db_session.query(LLMUsage).count() == 1
    with pytest.raises(ValueError, match="different message"):
        await service.complete_turn(
            session,
            "Different content",
            model="claude-sonnet-4",
            idempotency_key="stable-turn",
        )


@pytest.mark.asyncio
async def test_streaming_is_normalized_and_persisted_as_one_message(db_session):
    anthropic = _FakeProvider("anthropic", ["streamed reply"])
    google = _FakeProvider("google", [])
    service = _service(db_session, anthropic, google)
    session = Session(id="session-stream", messages=[])
    db_session.add(session)
    db_session.commit()

    chunks = [
        chunk
        async for chunk in service.stream_turn(
            session,
            "Stream this",
            model="claude-sonnet-4",
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
    anthropic = _FakeProvider(
        "anthropic",
        ["recovered"],
        failures=[TimeoutError("temporary")],
    )
    google = _FakeProvider("google", [])
    service = _service(db_session, anthropic, google, max_retries=1)
    session = Session(id="session-retry", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Retry me",
        model="claude-sonnet-4",
        idempotency_key="retry-turn",
    )

    assert result.content == "recovered"
    assert len(anthropic.calls) == 2
    assert [m["role"] for m in session.messages] == ["user", "assistant"]


def test_context_budget_keeps_newest_turns_and_system_prefix(db_session):
    service = CoordinatorService(
        db_session,
        providers={
            "anthropic": _FakeProvider("anthropic", []),
            "google": _FakeProvider("google", []),
        },
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


def test_provider_router_selects_adapter_by_model_name():
    anthropic = _FakeProvider("anthropic", [])
    google = _FakeProvider("google", [])
    router = ProviderRouter({"anthropic": anthropic, "google": google})

    assert router.get("claude-sonnet-4") is anthropic
    assert router.get("gemini-2.5-flash") is google
    with pytest.raises(ValueError, match="Cannot infer provider"):
        router.get("unknown-model")


def test_chat_endpoint_routes_requested_models_and_preserves_history(
    client,
    db_session,
    monkeypatch,
):
    anthropic = _FakeProvider("anthropic", ["first answer"])
    google = _FakeProvider("google", ["second answer"])
    monkeypatch.setitem(
        DEFAULT_PROVIDER_ROUTER.providers,
        "anthropic",
        anthropic,
    )
    monkeypatch.setitem(
        DEFAULT_PROVIDER_ROUTER.providers,
        "google",
        google,
    )

    first = client.post(
        "/api/chat",
        json={
            "thread_id": "api-switch-session",
            "message": "First question",
            "model": "claude-sonnet-4",
            "idempotency_key": "api-turn-1",
        },
    )
    second = client.post(
        "/api/chat",
        json={
            "thread_id": "api-switch-session",
            "message": "Second question",
            "model": "gemini-2.5-flash",
            "idempotency_key": "api-turn-2",
        },
    )

    assert first.status_code == second.status_code == 200
    assert '"type": "done"' in first.text
    assert '"type": "done"' in second.text
    assert [message["content"] for message in google.calls[0][1]] == [
        "First question",
        "first answer",
        "Second question",
    ]
    session = (
        db_session.query(Session)
        .filter(Session.thread_id == "api-switch-session")
        .one()
    )
    assert session.selected_provider == "google"
