import pytest
from dataclasses import dataclass

from app.db.models import Project, Session, Task
from app.services.coordinator import CoordinatorService
from app.services.providers import ProviderResponse


@dataclass
class _StreamingToolProvider:
    name: str = "openai"
    calls: int = 0

    async def complete(self, messages, model, stream=False, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(
                provider="openai",
                model=model,
                request_id="stream-tool-request",
                stop_reason="tool_calls",
                tool_calls=[
                    {"id": "call-status", "name": "get_status", "input": {}}
                ],
            )
        return ProviderResponse(
            provider="openai",
            model=model,
            text="There is one active project task.",
            request_id="stream-final-request",
            stop_reason="stop",
        )


@pytest.mark.asyncio
async def test_chat_executes_get_status_and_streams_tool_progress(
    db_session,
):
    db_session.add(Project(id="alpha", name="Alpha", status="active"))
    db_session.add(
        Task(
            id="ALPHA-001",
            project="alpha",
            title="Ship the release",
            status="dispatched",
            current_gate="dispatch",
        )
    )
    db_session.commit()

    provider = _StreamingToolProvider()
    service = CoordinatorService(db_session, providers={"openai": provider})

    session = Session(id="sess-tool", thread_id="tool-chat", title="Tool Chat")
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        message="Do I have any projects?",
        model="gpt-4o",
        provider="openai",
        idempotency_key="tool-chat-turn",
    )

    assert result.content == "There is one active project task."

    db_session.refresh(session)
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-status"

