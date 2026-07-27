from dataclasses import dataclass, field

from app.db.models import Project, Task
from app.services.coordinator import CoordinatorService
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse


@dataclass
class _ContextProvider:
    messages: list[list[dict]] = field(default_factory=list)

    async def complete(self, messages, model, stream=False, **kwargs):
        self.messages.append(messages)
        return ProviderResponse(
            provider="openai",
            model=model,
            text="context-aware",
            usage=UsageCounts(input_tokens=10, output_tokens=2),
            request_id="context-test",
            stop_reason="stop",
        )


def test_chat_prompt_contains_snapshot_and_refreshes_after_project_mutation(
    client,
    db_session,
    monkeypatch,
):
    db_session.add(Project(id="alpha", name="Alpha", status="active"))
    db_session.add(
        Task(
            id="ALPHA-001",
            project="alpha",
            title="Dispatch the release",
            status="dispatched",
        )
    )
    db_session.commit()

    provider = _ContextProvider()
    monkeypatch.setattr(
        "app.api.chat.CoordinatorService",
        lambda db: CoordinatorService(db, providers={"openai": provider}),
    )
    session = client.post(
        "/api/sessions",
        json={
            "thread_id": "context-chat",
            "project_id": "alpha",
            "context_level": "project",
        },
    )
    assert session.status_code == 201

    first = client.post(
        "/api/chat",
        json={
            "thread_id": "context-chat",
            "message": "What projects do I have?",
            "model": "gpt-4o",
            "idempotency_key": "context-turn-1",
        },
    )
    assert first.status_code == 200
    first_global = provider.messages[0][0]["content"]
    first_snapshot = next(
        message["content"] for message in provider.messages[0]
        if message["content"].startswith("## System State")
    )
    assert "- Projects: 1 active (Alpha)" in first_snapshot
    assert "ALPHA-001: Dispatch the release (dispatched)" in first_snapshot
    # The snapshot is its own message so mutations never touch the Global tier.
    assert "- Projects: 1 active (Alpha)" not in first_global

    updated = client.patch(
        "/api/projects/alpha",
        json={"name": "Renamed Alpha"},
    )
    assert updated.status_code == 200

    second = client.post(
        "/api/chat",
        json={
            "thread_id": "context-chat",
            "message": "Which project is this?",
            "model": "gpt-4o",
            "idempotency_key": "context-turn-2",
        },
    )
    assert second.status_code == 200
    second_global = provider.messages[1][0]["content"]
    second_snapshot = next(
        message["content"] for message in provider.messages[1]
        if message["content"].startswith("## System State")
    )
    assert "- Projects: 1 active (Renamed Alpha)" in second_snapshot
    # Global tier bytes are unaffected by the project mutation (stable prefix).
    assert second_global == first_global
