import pytest
from dataclasses import dataclass, field

from app.db.models import Project, Task, Session
from app.services import entity_admin
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


@pytest.mark.asyncio
async def test_chat_prompt_contains_snapshot_and_refreshes_after_project_mutation(
    db_session,
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
    service = CoordinatorService(db_session, providers={"openai": provider})
    
    session = Session(
        id="sess-context-chat",
        thread_id="context-chat",
        project_id="alpha",
        context_level="project",
    )
    db_session.add(session)
    db_session.commit()

    first = await service.complete_turn(
        session,
        message="What projects do I have?",
        model="gpt-4o",
        idempotency_key="context-turn-1",
    )
    assert first.content == "context-aware"
    first_global = provider.messages[0][0]["content"]
    first_snapshot = next(
        message["content"] for message in provider.messages[0]
        if message["content"].startswith("## System State")
    )
    assert "- Projects: 1 active (Alpha)" in first_snapshot
    assert "ALPHA-001: Dispatch the release (dispatched)" in first_snapshot
    assert "- Projects: 1 active (Alpha)" not in first_global

    entity_admin.update_project(db_session, "alpha", {"name": "Renamed Alpha"})

    second = await service.complete_turn(
        session,
        message="Which project is this?",
        model="gpt-4o",
        idempotency_key="context-turn-2",
    )
    assert second.content == "context-aware"
    second_global = provider.messages[1][0]["content"]
    second_snapshot = next(
        message["content"] for message in provider.messages[1]
        if message["content"].startswith("## System State")
    )
    assert "- Projects: 1 active (Renamed Alpha)" in second_snapshot
    assert second_global == first_global

