from dataclasses import dataclass
import json

from app.db.models import Project, Session, Task
from app.services.coordinator import DEFAULT_PROVIDER_ROUTER
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


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def test_chat_executes_get_status_and_streams_tool_progress(
    client,
    db_session,
    monkeypatch,
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
    monkeypatch.setitem(DEFAULT_PROVIDER_ROUTER.providers, "openai", provider)

    response = client.post(
        "/api/chat",
        json={
            "thread_id": "tool-chat",
            "message": "Do I have any projects?",
            "model": "gpt-4o",
            "provider": "openai",
            "idempotency_key": "tool-chat-turn",
        },
    )

    assert response.status_code == 200
    events = _events(response.text)
    assert any(event["type"] == "tool_call" for event in events)
    assert any(event["type"] == "tool_result" for event in events)
    assert events[-1] == {
        "type": "done",
        "id": events[-1]["id"],
        "content": "There is one active project task.",
    }

    session = (
        db_session.query(Session)
        .filter(Session.thread_id == "tool-chat")
        .one()
    )
    tool_messages = [message for message in session.messages if message["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call-status"
