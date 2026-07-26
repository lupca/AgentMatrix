from dataclasses import dataclass, field
from copy import deepcopy

import pytest

from app.db.models import Project, Session, Task
from app.services.coordinator import CoordinatorService
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse


@dataclass
class _ToolProvider:
    responses: list[ProviderResponse]
    calls: list[list[dict]] = field(default_factory=list)

    name: str = "openai"

    async def complete(self, messages, model, stream=False, **kwargs):
        self.calls.append(deepcopy(messages))
        response = self.responses.pop(0)
        return response


@pytest.mark.asyncio
async def test_api_tool_calls_execute_and_persist_results(db_session):
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

    provider = _ToolProvider(
        responses=[
            ProviderResponse(
                provider="openai",
                model="gpt-4o",
                request_id="tool-request",
                stop_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call-status",
                        "name": "get_status",
                        "input": {},
                    }
                ],
                usage=UsageCounts(input_tokens=10, output_tokens=2),
            ),
            ProviderResponse(
                provider="openai",
                model="gpt-4o",
                text="Alpha is dispatched and ready for review.",
                request_id="final-request",
                stop_reason="stop",
                usage=UsageCounts(input_tokens=20, output_tokens=8),
            ),
        ]
    )
    service = CoordinatorService(
        db_session,
        providers={"openai": provider},
        retry_base_seconds=0,
    )
    session = Session(id="tool-session", thread_id="tool-session", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "What projects do I have?",
        model="gpt-4o",
        provider="openai",
        idempotency_key="tool-turn",
    )

    assert result.content == "Alpha is dispatched and ready for review."
    assert len(provider.calls) == 2
    assert provider.calls[1][-2]["role"] == "assistant"
    assert provider.calls[1][-2]["tool_calls"][0]["id"] == "call-status"
    assert provider.calls[1][-1] == {
        "role": "tool",
        "tool_call_id": "call-status",
        "name": "get_status",
        "content": '{"status": "success", "tasks": [{"id": "ALPHA-001", "title": "Ship the release", "project": "alpha", "status": "dispatched", "current_gate": "dispatch", "executor": null, "reviewer": null}]}',
    }

    persisted_roles = [message["role"] for message in session.messages]
    assert persisted_roles == ["user", "assistant", "tool", "assistant"]
    assert session.messages[2]["tool_call_id"] == "call-status"
    assert session.messages[2]["status"] == "complete"
