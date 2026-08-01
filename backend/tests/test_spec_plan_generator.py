import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from app.db.models import Task
from app.services.llm_service import ConfigurationError
from app.services.providers import ProviderResponse
from app.services.spec_plan_generator import (
    SPEC_PLAN_RESULT_SCHEMA_VERSION,
    SpecPlanGenerationError,
    generate_spec_plan,
)


def _task() -> Task:
    return Task(id="SPEC-GEN-1", project="proj", title="Build the widget")


def _agent():
    return SimpleNamespace(id="spec-agent", agent_type="api", provider="openai", model="gpt-4o")


def _response(content: str) -> ProviderResponse:
    return ProviderResponse(provider="openai", model="gpt-4o", text=content)


def _valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": SPEC_PLAN_RESULT_SCHEMA_VERSION,
        "acceptance_criteria": ["Widget renders", "Widget has tests"],
        "plan": "1. Build widget. 2. Test widget.",
        "files": ["backend/app/widget.py", "backend/app/made_up.py"],
        "tests": ["backend/tests/test_widget.py"],
        "risk": "low",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_generate_spec_plan_marks_unconfirmed_files_and_uses_graph_flows():
    task = _task()
    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(return_value=[{"file_path": "backend/app/widget.py"}]),
    ), patch(
        "app.services.spec_plan_generator.get_affected_flows",
        new=AsyncMock(return_value=["checkout-flow"]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload()))),
    ):
        result, flows = await generate_spec_plan(task, "/tmp/repo", _agent())

    assert result.acceptance_criteria == ["Widget renders", "Widget has tests"]
    assert result.files == [
        "backend/app/widget.py",
        "backend/app/made_up.py *(chưa xác nhận)*",
    ]
    assert flows == ["checkout-flow"]


@pytest.mark.asyncio
async def test_generate_spec_plan_without_repo_root_marks_all_files_unconfirmed():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload()))),
    ):
        result, flows = await generate_spec_plan(_task(), None, _agent())

    assert all(f.endswith("*(chưa xác nhận)*") for f in result.files)
    assert flows == []


@pytest.mark.asyncio
async def test_generate_spec_plan_retries_once_on_invalid_json_then_succeeds():
    responses = iter(["not json at all", json.dumps(_valid_payload())])

    mock_complete = AsyncMock(side_effect=lambda *_args, **_kwargs: _response(next(responses)))
    with patch("app.services.spec_plan_generator.LLMService.complete", new=mock_complete):
        result, _flows = await generate_spec_plan(_task(), None, _agent())

    assert mock_complete.call_count == 2
    assert result.plan == "1. Build widget. 2. Test widget."


@pytest.mark.asyncio
async def test_generate_spec_plan_raises_after_repeated_schema_failures():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response("still not json")),
    ), pytest.raises(SpecPlanGenerationError):
        await generate_spec_plan(_task(), None, _agent())


@pytest.mark.asyncio
async def test_generate_spec_plan_rejects_empty_acceptance_criteria():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload(acceptance_criteria=[])))),
    ), pytest.raises(SpecPlanGenerationError):
        await generate_spec_plan(_task(), None, _agent())


@pytest.mark.asyncio
async def test_generate_spec_plan_uses_the_passed_agent():
    agent = _agent()
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload()))),
    ) as mock_complete:
        await generate_spec_plan(_task(), None, agent)

    assert mock_complete.call_args.args[0] is agent


@pytest.mark.asyncio
async def test_generate_spec_plan_requires_an_agent():
    with pytest.raises(ConfigurationError):
        await generate_spec_plan(_task(), None, None)


def test_parse_json_strips_reasoning_think_block():
    from app.services.spec_plan_generator import _parse_json

    wrapped = '<think>let me reason\nabout this</think>{"a": 1}'
    assert _parse_json(wrapped) == {"a": 1}


def test_parse_json_extracts_object_from_surrounding_prose():
    from app.services.spec_plan_generator import _parse_json

    prose = 'Here is the plan you asked for:\n{"a": [1, 2]}\nHope it helps!'
    assert _parse_json(prose) == {"a": [1, 2]}


def test_prompt_includes_description_context_and_quality_bars():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(
        id="T-1", project="p1", title="Add login rate limiting",
        raw_input="Limit to 5 attempts per minute per IP using the existing redis client.",
    )
    prompt = _build_prompt(
        task, ["app/auth.py"],
        project_context="# Project: p1\n## Hard Boundaries\n- never bypass the gate ledger",
    )
    assert "existing redis client" in prompt          # raw_input reaches the planner
    assert "never bypass the gate ledger" in prompt   # project context injected
    assert "objectively verifiable" in prompt         # anti-vacuous AC bar
    assert "Open questions" in prompt                 # thin-input escape hatch
    assert "Scope" in prompt


def test_prompt_without_description_or_context_still_valid():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-2", project="p1", title="Tiny fix")
    prompt = _build_prompt(task, [])
    assert "Task description:" not in prompt
    assert "Project context" not in prompt
