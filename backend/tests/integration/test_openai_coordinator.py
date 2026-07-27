from types import SimpleNamespace

import pytest

from app.db.models import LLMUsage, Session
from app.services.coordinator import CoordinatorService
from app.services.providers.api_provider import APIProvider


class _Completions:
    async def create(self, **request):
        assert request["model"] == "gpt-4o"
        return SimpleNamespace(
            id="openai-integration-response",
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="OpenAI coordinator reply"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=40, completion_tokens=8),
        )


@pytest.mark.asyncio
async def test_coordinator_completes_with_mock_openai_api(db_session):
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    service = CoordinatorService(
        db_session,
        providers={"openai": APIProvider(client=client)},
        retry_base_seconds=0,
    )
    session = Session(id="openai-integration-session", messages=[])
    db_session.add(session)
    db_session.commit()

    result = await service.complete_turn(
        session,
        "Use OpenAI",
        model="gpt-4o",
        idempotency_key="openai-integration-turn",
    )

    assert result.provider == "openai"
    assert result.content == "OpenAI coordinator reply"
    usage = db_session.query(LLMUsage).one()
    assert usage.provider == "openai"
    assert usage.input_tokens == 40
    assert usage.output_tokens == 8
