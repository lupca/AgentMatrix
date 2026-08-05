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
    spec_plan_result_from_task,
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


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"a": 1}\nDone. I reviewed the plan.', id="trailing_prose"),
        pytest.param('{"a": 1}\n{"ignored": true}', id="trailing_second_object"),
        pytest.param('{"a": 1}\n\n', id="trailing_whitespace"),
    ],
)
def test_parse_json_ignores_content_after_the_first_object(raw):
    """Output that *starts* with a valid object must not die on what follows.

    The old recovery was gated on ``not text.startswith("{")``, so this exact
    shape skipped recovery and raised "Extra data: line 2 column 1". It took
    down the agy plan critic on VOMA-033 three times in a row while the claude
    planner — same parser, no trailing prose — passed, which is why the failure
    looked CLI-specific rather than like a parser bug.
    """
    from app.services.spec_plan_generator import _parse_json

    assert _parse_json(raw) == {"a": 1}


def test_parse_json_picks_the_right_brace_with_prose_on_both_sides():
    """A '}' inside trailing prose must not extend the parsed span.

    The previous rfind("}") span swallowed it and produced garbage instead of
    failing, which is worse than either correct parsing or a clean error.
    """
    from app.services.spec_plan_generator import _parse_json

    raw = 'Review below:\n{"a": 1}\nSee the } in this sentence.'
    assert _parse_json(raw) == {"a": 1}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("I could not complete the review.", id="no_json_at_all"),
        pytest.param("[1, 2, 3]", id="json_array_not_object"),
        pytest.param('{"a": 1', id="truncated_object"),
    ],
)
def test_parse_json_still_rejects_unusable_output(raw):
    """Recovery must not turn a real failure into silence.

    A critic that returned no usable object has not reviewed anything; the
    caller retries on JSONDecodeError, so failing loudly here is the point.
    """
    from app.services.spec_plan_generator import _parse_json

    with pytest.raises(json.JSONDecodeError):
        _parse_json(raw)


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
    assert '`spec_get({"filter":{"project_id":"p1"}})`' in prompt
    assert "load_tools" not in prompt
    priorities = [
        prompt.index("1. Negative boundaries"),
        prompt.index("2. Existing system"),
        prompt.index("3. Code location"),
        prompt.index("4. Delivery history"),
    ]
    assert priorities == sorted(priorities)
    assert "prior_art PHẢI dẫn `spec_item:<id>`" in prompt
    assert "constraints PHẢI nêu ranh giới đó" in prompt


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
    assert "MUST first call the MCP tool" in prompt
    assert '`spec_get({"filter":{"project_id":"proj"}})`' in prompt
    assert "load_tools" not in prompt
    assert "before evaluating prior_art" in prompt
    assert "concrete spec_item id" in prompt
    assert "you may query spec_item/spec_task_link" not in prompt
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


@pytest.mark.asyncio
async def test_generate_spec_plan_warns_when_graph_raises():
    """Graph exception must NOT be silent — plan is generated but flagged."""
    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(side_effect=RuntimeError("graph MCP unreachable")),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(
            _valid_payload(spec_clarity="high", open_questions=[])
        ))),
    ):
        result, flows = await generate_spec_plan(_task(), "/tmp/repo", _agent())

    assert result.spec_clarity == "low"
    assert any("without repository grounding" in q for q in result.open_questions)
    assert flows == []


@pytest.mark.asyncio
async def test_generate_spec_plan_warns_when_graph_returns_empty():
    """Empty graph results must also flag the plan as ungrounded."""
    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(return_value=[]),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(
            _valid_payload(spec_clarity="medium", open_questions=[])
        ))),
    ):
        result, _ = await generate_spec_plan(_task(), "/tmp/repo", _agent())

    assert result.spec_clarity == "low"
    assert any("without repository grounding" in q for q in result.open_questions)


@pytest.mark.asyncio
async def test_generate_spec_plan_graph_error_preserves_existing_low_clarity():
    """If the LLM already set spec_clarity='low', post-process keeps it."""
    with patch(
        "app.services.spec_plan_generator.semantic_search",
        new=AsyncMock(side_effect=ConnectionError("refused")),
    ), patch(
        "app.services.spec_plan_generator.LLMService.complete",
        new=AsyncMock(return_value=_response(json.dumps(
            _valid_payload(spec_clarity="low", open_questions=["already unclear"])
        ))),
    ):
        result, _ = await generate_spec_plan(_task(), "/tmp/repo", _agent())

    assert result.spec_clarity == "low"
    assert "already unclear" in result.open_questions
    assert any("without repository grounding" in q for q in result.open_questions)


def test_build_search_query_combines_raw_input_and_title():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_search_query

    task = Task(
        id="Q-1", project="p", title="Fix auth bug",
        raw_input="Rate limiter in app/auth.py bypassed when X-Forwarded-For is spoofed",
    )
    query = _build_search_query(task)
    assert "Rate limiter" in query
    assert "Fix auth bug" in query


def test_build_search_query_deduplicates_title_in_raw_input():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_search_query

    task = Task(
        id="Q-2", project="p", title="Fix auth bug",
        raw_input="Fix auth bug in the login flow",
    )
    query = _build_search_query(task)
    # title is a substring of raw_input (case-insensitive), so not duplicated
    assert query.lower().count("fix auth bug") == 1


def test_build_prompt_includes_graph_warning():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-3", project="p1", title="Something")
    prompt = _build_prompt(task, [], graph_warning="graph is broken")
    assert "graph is broken" in prompt
    assert "spec_clarity" in prompt


def test_build_prompt_no_warning_when_graph_ok():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-4", project="p1", title="Something")
    prompt = _build_prompt(task, ["app/foo.py"])
    assert "IMPORTANT" not in prompt


def test_build_prompt_includes_prior_round_task_plan():
    """CTV2-1376: task.plan must reach the planner so coordinator answers to
    open_questions (placed there via update_task) are visible on the next
    generate_spec_plan call.  Without this the planner re-asks the same
    questions every round, burning ~98k tokens per loop."""
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    distinctive_constraint = "Do not remove or migrate claimed_by_session_id"
    task = Task(
        id="T-PLAN-1",
        project="p1",
        title="Prior plan visibility",
        plan=(
            "Intent: preserve existing session column.\n"
            f"Scope — in: auth.py; out: coordinator.py\n"
            f"1. {distinctive_constraint}\n"
            "2. Delete the wake path entirely; do not retain a no-op\n"
            "Open questions: none"
        ),
    )
    prompt = _build_prompt(task, ["app/auth.py"])

    assert "YOUR OWN PRIOR-ROUND PLAN" in prompt
    assert "task.plan" in prompt
    assert distinctive_constraint in prompt
    assert "Delete the wake path entirely" in prompt


def test_build_prompt_omits_prior_plan_block_when_task_plan_is_empty():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-PLAN-2", project="p1", title="No prior plan")
    prompt = _build_prompt(task, [])
    assert "YOUR OWN PRIOR-ROUND PLAN" not in prompt


def test_build_prompt_labels_plan_and_coordinator_notes_distinctly():
    """CTV2-1397: task.plan (planner's own prior output) and
    task.coordinator_notes (coordinator's command) must be labeled so the
    planner never mistakes its own draft for an instruction, and vice versa."""
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(
        id="T-PLAN-NOTES-1",
        project="p1",
        title="Distinguish plan from notes",
        plan="Prior draft: implement via approach A.",
        coordinator_notes="Use approach B instead, per the security review.",
    )
    prompt = _build_prompt(task, [])

    assert "YOUR OWN PRIOR-ROUND PLAN" in prompt
    assert "Prior draft: implement via approach A." in prompt
    assert "COORDINATOR DIRECTION" in prompt
    assert "COMMAND FROM THE COORDINATOR, NOT YOUR OWN DRAFT" in prompt
    assert "Use approach B instead, per the security review." in prompt
    # The coordinator's directive text must appear after (i.e. takes
    # precedence framing over) the planner's own prior draft.
    assert prompt.index("YOUR OWN PRIOR-ROUND PLAN") < prompt.index(
        "COORDINATOR DIRECTION"
    )


def test_build_prompt_omits_coordinator_notes_block_when_empty():
    from app.db.models import Task
    from app.services.spec_plan_generator import _build_prompt

    task = Task(id="T-PLAN-NOTES-2", project="p1", title="No notes")
    prompt = _build_prompt(task, [])
    assert "COORDINATOR DIRECTION" not in prompt


def test_build_prompt_truncates_oversized_task_plan():
    from app.db.models import Task
    from app.services.spec_plan_generator import (
        _PRIOR_PLAN_MAX_CHARS,
        _build_prompt,
    )

    oversized = "x" * (_PRIOR_PLAN_MAX_CHARS + 5_000)
    task = Task(id="T-PLAN-3", project="p1", title="Big plan", plan=oversized)
    prompt = _build_prompt(task, [])
    assert "truncated to fit 25KB cap" in prompt
    assert len(prompt) < len(oversized) + 5_000


def test_spec_plan_result_from_task_round_trips_a_written_plan():
    task = Task(
        id="SPEC-RT-1",
        project="proj",
        title="Build the widget",
        acceptance_criteria=["Endpoint returns 200"],
        constraints=["Do not add a migration"],
        evidence=[{
            "fact": "Relevant module exists",
            "source_type": "file",
            "source": "backend/app/example.py:1",
            "result": "module exists",
        }],
        prior_art=["Prior task did X"],
        ruled_out=[{"approach": "Rewrite everything", "reason": "too risky"}],
        limits=None,
        plan="1. Add route. 2. Add tests.",
        files=["backend/app/api/foo.py"],
        tests=["backend/tests/test_foo.py"],
        risk="low",
        spec_clarity="high",
        open_questions=[],
    )

    result = spec_plan_result_from_task(task)

    assert result.schema_version == SPEC_PLAN_RESULT_SCHEMA_VERSION
    assert result.acceptance_criteria == ["Endpoint returns 200"]
    assert result.constraints == ["Do not add a migration"]
    assert result.evidence[0].fact == "Relevant module exists"
    assert result.plan == "1. Add route. 2. Add tests."
    assert result.files == ["backend/app/api/foo.py"]
    assert result.risk == "low"
    assert result.spec_clarity == "high"


def test_spec_plan_result_from_task_errors_clearly_on_unusable_row():
    task = Task(id="SPEC-RT-2", project="proj", title="No plan written yet")

    with pytest.raises(PlanCriticError):
        spec_plan_result_from_task(task)


@pytest.mark.parametrize(
    "raw,expected",
    [
        pytest.param('{"a": 1, "plan": "viet do dang', "looks_truncated", id="cut_mid_string"),
        pytest.param('{"a": 1, "b": 2', "looks_truncated", id="cut_mid_object"),
        pytest.param('{"a": 1,', "looks_truncated", id="cut_after_comma"),
        pytest.param('{"a": 1 "b": 2}', "looks_malformed", id="missing_comma"),
        pytest.param("{'a': 1}", "looks_malformed", id="single_quotes"),
    ],
)
def test_parse_failure_separates_a_cut_stream_from_bad_json(raw, expected):
    """A truncated stream and a model writing bad JSON raise the SAME exception.

    Two planner runs died mid-JSON on 2026-08-04 ("Expecting ',' delimiter" at
    char 21626 and 20614) and there was no way to tell which had happened, so
    the cause stayed a guess. Classification keys off the tail: a complete JSON
    document ends with its closing brace, a cut stream does not.

    Offset-based classification does NOT work here — for an unterminated string
    json reports the position where the string *opened*, so a cut stream still
    looks like it has plenty of characters left.
    """
    from app.services.spec_plan_generator import _describe_parse_failure

    with pytest.raises(json.JSONDecodeError) as excinfo:
        json.loads(raw)

    detail = _describe_parse_failure(excinfo.value, raw, run_id="run-under-test")
    assert expected in detail
    assert f"raw_len={len(raw)}" in detail


def test_prior_art_accepts_structured_spec_item_citations():
    """A dict citation must not throw away an otherwise-good plan.

    CTV2-1388, 2026-08-05: the planner answered `prior_art` with the shape it
    had just read from the living spec -- `{"spec_item": "...", "note": "..."}`
    -- and `strict=True` rejected all five entries, failing a 215-second run
    whose content was fine.
    """

    from app.schemas.task import SpecPlanResult

    payload = _valid_payload(
        prior_art=[
            {
                "spec_item": "spec_item:1779ef5f-de15-4b3c-8dd5-c08825b055bf",
                "note": "GateRecord append-only ledger",
            },
            "A plain string still works",
        ]
    )

    result = SpecPlanResult.model_validate(payload)

    assert result.prior_art == [
        "spec_item:1779ef5f-de15-4b3c-8dd5-c08825b055bf — GateRecord append-only ledger",
        "A plain string still works",
    ]


def test_strictness_still_holds_where_structure_matters():
    """Only prior_art is lenient; the rest of the contract is unchanged."""

    import pytest as _pytest
    from pydantic import ValidationError

    from app.schemas.task import SpecPlanResult

    with _pytest.raises(ValidationError):
        SpecPlanResult.model_validate(
            _valid_payload(acceptance_criteria=[{"text": "not a string"}])
        )
