import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from app.db.models import AgentRun, LLMUsage, Project, Task
from app.services.llm_service import ConfigurationError
from app.services.providers import ProviderResponse
from app.services.spec_plan_generator import (
    PLAN_CRITIC_TOKEN_BUDGET,
    SPEC_PLAN_RESULT_SCHEMA_VERSION,
    PlanCriticError,
    SpecPlanGenerationError,
    criticize_spec_plan,
    generate_spec_plan,
)


def _task() -> Task:
    return Task(id="SPEC-GEN-1", project="proj", title="Build the widget")


def _agent():
    return SimpleNamespace(
        id="spec-agent",
        agent_type="cli",
        provider="openai",
        cli="codex",
        model="gpt-4o",
    )


def _critic_agent():
    return SimpleNamespace(
        id="critic-agent",
        agent_type="cli",
        provider="anthropic",
        cli="claude",
        model="claude-sonnet",
    )


def _response(content: str) -> ProviderResponse:
    return ProviderResponse(provider="openai", model="gpt-4o", text=content)


def _valid_payload(**overrides) -> dict:
    payload = {
        "schema_version": SPEC_PLAN_RESULT_SCHEMA_VERSION,
        "acceptance_criteria": ["Widget renders", "Widget has tests"],
        "constraints": ["Do not add a database migration"],
        "evidence": [{
            "fact": "Widget module exists",
            "source_type": "file",
            "source": "backend/app/widget.py:1",
            "result": "module docstring declares widget support",
        }],
        "prior_art": ["Existing widget rendering helper"],
        "ruled_out": [{"approach": "Replace renderer", "reason": "Breaks callers"}],
        "limits": None,
        "plan": "1. Build widget. 2. Test widget.",
        "files": ["backend/app/widget.py", "backend/app/made_up.py"],
        "tests": ["backend/tests/test_widget.py"],
        "risk": "low",
        "spec_clarity": "high",
        "open_questions": [],
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
async def test_generate_spec_plan_with_no_graph_matches_marks_all_files_unconfirmed():
    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload()))),
    ):
        result, flows = await generate_spec_plan(_task(), "/tmp/repo", _agent())

    assert all(f.endswith("*(chưa xác nhận)*") for f in result.files)
    assert flows == []


@pytest.mark.asyncio
async def test_generate_spec_plan_retries_once_on_invalid_json_then_succeeds():
    responses = iter(["not json at all", json.dumps(_valid_payload())])

    mock_complete = AsyncMock(side_effect=lambda *_args, **_kwargs: _response(next(responses)))
    with patch("app.services.spec_plan_generator.LLMService.complete", new=mock_complete):
        result, _flows = await generate_spec_plan(_task(), "/tmp/repo", _agent())

    assert mock_complete.call_count == 2
    assert result.plan == "1. Build widget. 2. Test widget."


@pytest.mark.asyncio
async def test_generate_spec_plan_raises_after_repeated_schema_failures():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response("still not json")),
    ), pytest.raises(SpecPlanGenerationError):
        await generate_spec_plan(_task(), "/tmp/repo", _agent())


@pytest.mark.asyncio
async def test_generate_spec_plan_allows_constraints_only():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload(acceptance_criteria=[])))),
    ):
        result, _ = await generate_spec_plan(_task(), "/tmp/repo", _agent())
    assert result.acceptance_criteria == []
    assert result.constraints


@pytest.mark.asyncio
async def test_generate_spec_plan_rejects_empty_acceptance_and_constraints():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload(
            acceptance_criteria=[], constraints=[]
        )))),
    ), pytest.raises(SpecPlanGenerationError):
        await generate_spec_plan(_task(), "/tmp/repo", _agent())


@pytest.mark.asyncio
async def test_generate_spec_plan_uses_the_passed_agent():
    agent = _agent()
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(_valid_payload()))),
    ) as mock_complete:
        await generate_spec_plan(_task(), "/tmp/repo", agent)

    assert mock_complete.call_args.args[0] is agent
    assert mock_complete.call_args.kwargs["cwd"] == "/tmp/repo"


@pytest.mark.asyncio
async def test_generate_spec_plan_requires_an_agent():
    with pytest.raises(ConfigurationError):
        await generate_spec_plan(_task(), "/tmp/repo", None)


@pytest.mark.asyncio
async def test_planner_and_critic_requests_are_recorded_as_agent_runs(db_session, tmp_path):
    task = Task(id="SPEC-RUN-1", project="proj", title="Build the widget")
    db_session.add(Project(id="proj", name="Project", repo_root=str(tmp_path)))
    db_session.add(task)
    db_session.commit()
    payload = _valid_payload()
    plan_response = _response(json.dumps(payload))
    critic_response = _response(json.dumps({
        "schema_version": "1.0",
        "verdict": "accept",
        "findings": [],
        "summary": "Citations reproduced.",
    }))

    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(side_effect=[plan_response, critic_response]),
    ):
        await generate_spec_plan(task, str(tmp_path), _agent(), db=db_session)
        await criticize_spec_plan(
            task,
            __import__("app.schemas.task", fromlist=["SpecPlanResult"]).SpecPlanResult.model_validate(payload),
            str(tmp_path),
            _agent(),
            _critic_agent(),
            db=db_session,
        )

    runs = db_session.query(AgentRun).order_by(AgentRun.created_at).all()
    usages = db_session.query(LLMUsage).all()
    assert [run.kind for run in runs] == ["execute", "review"]
    assert all(run.status == "success" for run in runs)
    assert {usage.operation for usage in usages} == {"plan", "plan_critic"}
    assert {usage.agent_run_id for usage in usages} == {run.id for run in runs}


@pytest.mark.asyncio
async def test_generate_spec_plan_requires_repo_root():
    with pytest.raises(ConfigurationError, match="repo_root"):
        await generate_spec_plan(_task(), None, _agent())


@pytest.mark.asyncio
async def test_generate_spec_plan_rejects_api_agent_before_llm_call():
    agent = SimpleNamespace(id="@api-spec", agent_type="api")
    with patch(
        "app.services.spec_plan_generator.LLMService.complete", new=AsyncMock()
    ) as mock_complete, pytest.raises(
        ConfigurationError,
        match=(
            "Spec/plan research requires a CLI agent that can read the repository; "
            "@api-spec is API-backed"
        ),
    ):
        await generate_spec_plan(_task(), "/tmp/repo", agent)
    mock_complete.assert_not_awaited()


@pytest.mark.parametrize("missing", [
    "constraints", "evidence", "prior_art", "ruled_out", "limits",
    "spec_clarity", "open_questions",
])
def test_spec_plan_result_v2_requires_contract_fields(missing):
    payload = _valid_payload()
    payload.pop(missing)
    with pytest.raises(ValidationError):
        from app.schemas.task import SpecPlanResult

        SpecPlanResult.model_validate(payload)


def test_spec_plan_result_v2_accepts_complete_strict_payload():
    from app.schemas.task import SpecPlanResult

    result = SpecPlanResult.model_validate(_valid_payload())
    assert result.schema_version == "2.0"
    assert result.spec_clarity == "high"
    assert result.open_questions == []


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
    assert "ĐỌC (read-only, không sửa gì)" in prompt
    assert '"spec_clarity"' in prompt
    assert '"open_questions"' in prompt
    assert '"constraints"' in prompt
    assert '"evidence"' in prompt
    assert "exact command plus its observed output" in prompt


def test_high_risk_plan_requires_enforced_limits():
    from app.schemas.task import SpecPlanResult

    with pytest.raises(ValidationError, match="limits are required"):
        SpecPlanResult.model_validate(_valid_payload(risk="high", limits=None))
    result = SpecPlanResult.model_validate(_valid_payload(
        risk="high",
        limits={"max_execution_rounds": 2, "max_tokens": 100_000, "max_cost_usd": 5.0},
    ))
    assert result.limits.max_execution_rounds == 2


@pytest.mark.asyncio
async def test_plan_critic_is_independent_focused_and_budgeted():
    critic_payload = {
        "schema_version": "1.0",
        "verdict": "accept",
        "findings": [],
        "summary": "Citations reproduced.",
    }
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(critic_payload))),
    ) as complete:
        result, tokens = await criticize_spec_plan(
            _task(),
            __import__("app.schemas.task", fromlist=["SpecPlanResult"]).SpecPlanResult.model_validate(
                _valid_payload()
            ),
            "/tmp/repo",
            _agent(),
            _critic_agent(),
        )
    assert result.verdict == "accept"
    assert tokens < PLAN_CRITIC_TOKEN_BUDGET
    prompt = complete.call_args.args[1][0]["content"]
    assert "MUST NOT run git diff" in prompt
    assert "spec_item/spec_task_link" in prompt
    assert complete.call_args.kwargs["max_tokens"] <= 4096


@pytest.mark.asyncio
async def test_plan_critic_cannot_reject_without_evidence():
    invalid = {
        "schema_version": "1.0",
        "verdict": "reject",
        "findings": [],
        "summary": "Looks wrong.",
    }
    with patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(invalid))),
    ), pytest.raises(PlanCriticError):
        await criticize_spec_plan(
            _task(),
            __import__("app.schemas.task", fromlist=["SpecPlanResult"]).SpecPlanResult.model_validate(
                _valid_payload()
            ),
            "/tmp/repo",
            _agent(),
            _critic_agent(),
        )


@pytest.mark.asyncio
async def test_plan_critic_enforces_four_eyes_before_calling_model():
    with patch(
        "app.services.spec_plan_generator.LLMService.complete", new=AsyncMock()
    ) as complete, pytest.raises(ConfigurationError, match="four-eyes"):
        await criticize_spec_plan(
            _task(),
            __import__("app.schemas.task", fromlist=["SpecPlanResult"]).SpecPlanResult.model_validate(
                _valid_payload()
            ),
            "/tmp/repo",
            _agent(),
            _agent(),
        )
    complete.assert_not_awaited()


def test_prompt_without_description_or_context_still_valid():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-2", project="p1", title="Tiny fix")
    prompt = _build_prompt(task, [])
    assert "Task description:" not in prompt
    assert "Project context" not in prompt
