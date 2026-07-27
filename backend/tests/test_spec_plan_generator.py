import json
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import Task
from app.services.spec_plan_generator import (
    SPEC_PLAN_RESULT_SCHEMA_VERSION,
    SpecPlanGenerationError,
    generate_spec_plan,
)


def _task() -> Task:
    return Task(id="SPEC-GEN-1", project="proj", title="Build the widget")


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
    ), patch.object(
        __import__("app.services.llm_client", fromlist=["LLMClient"]).LLMClient,
        "complete",
        return_value=json.dumps(_valid_payload()),
    ):
        result, flows = await generate_spec_plan(task, "/tmp/repo")

    assert result.acceptance_criteria == ["Widget renders", "Widget has tests"]
    assert result.files == [
        "backend/app/widget.py",
        "backend/app/made_up.py *(chưa xác nhận)*",
    ]
    assert flows == ["checkout-flow"]


@pytest.mark.asyncio
async def test_generate_spec_plan_without_repo_root_marks_all_files_unconfirmed():
    task = _task()
    with patch.object(
        __import__("app.services.llm_client", fromlist=["LLMClient"]).LLMClient,
        "complete",
        return_value=json.dumps(_valid_payload()),
    ):
        result, flows = await generate_spec_plan(task, None)

    assert all(f.endswith("*(chưa xác nhận)*") for f in result.files)
    assert flows == []


@pytest.mark.asyncio
async def test_generate_spec_plan_retries_once_on_invalid_json_then_succeeds():
    task = _task()
    responses = iter(["not json at all", json.dumps(_valid_payload())])
    with patch.object(
        __import__("app.services.llm_client", fromlist=["LLMClient"]).LLMClient,
        "complete",
        side_effect=lambda *a, **k: next(responses),
    ) as mock_complete:
        result, _flows = await generate_spec_plan(task, None)

    assert mock_complete.call_count == 2
    assert result.plan == "1. Build widget. 2. Test widget."


@pytest.mark.asyncio
async def test_generate_spec_plan_raises_after_repeated_schema_failures():
    task = _task()
    with patch.object(
        __import__("app.services.llm_client", fromlist=["LLMClient"]).LLMClient,
        "complete",
        return_value="still not json",
    ):
        with pytest.raises(SpecPlanGenerationError):
            await generate_spec_plan(task, None)


@pytest.mark.asyncio
async def test_generate_spec_plan_rejects_empty_acceptance_criteria():
    task = _task()
    with patch.object(
        __import__("app.services.llm_client", fromlist=["LLMClient"]).LLMClient,
        "complete",
        return_value=json.dumps(_valid_payload(acceptance_criteria=[])),
    ):
        with pytest.raises(SpecPlanGenerationError):
            await generate_spec_plan(task, None)
