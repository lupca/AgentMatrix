import json

import pytest

from app.db.models import Agent, AgentRun, LLMUsage, Project, RunResourceUsage, Task
from app.services.command_router import CommandRouter
from app.services.task_validators import TaskValidator
from app.workers.cli_executor import _record_cli_usage, _record_run_resource_usage
from app.workers.output_parser import _extract_explicit_result_ref, parse_cli_token_usage


CLAUDE_OUTPUT = json.dumps(
    {
        "type": "result",
        "usage": {
            "input_tokens": 2,
            "output_tokens": 14,
            "cache_creation_input_tokens": 13120,
            "cache_read_input_tokens": 15273,
        },
        "total_cost_usd": 0.139,
        "duration_ms": 2469,
    }
)

CLAUDE_MULTI_MODEL_OUTPUT = json.dumps(
    {
        "is_error": False,
        "duration_api_ms": 909970,
        "num_turns": 118,
        "stop_reason": "end_turn",
        "total_cost_usd": 6.175989600000001,
        "usage": {
            "input_tokens": 224,
            "cache_creation_input_tokens": 172344,
            "cache_read_input_tokens": 13659112,
            "output_tokens": 69332,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"costUSD": 0.0035399999999999997},
            "claude-sonnet-5": {"costUSD": 6.172449600000001},
        },
        "permission_denials": [],
        "terminal_reason": "completed",
        "result": "completed",
    }
)

QWEN_OUTPUT = json.dumps(
    {
        "usage": {
            "input_tokens": 49714,
            "output_tokens": 165,
            "cache_read_input_tokens": 0,
            "total_tokens": 49879,
        },
        "duration_ms": 11724,
        "stats": {
            "models": {
                "qwen3.7-plus": {
                    "tokens": {"prompt": 49714, "candidates": 165, "total": 49879, "cached": 0}
                }
            }
        },
    }
)

AGY_OUTPUT = json.dumps(
    {
        "usage": {
            "input_tokens": 10249,
            "output_tokens": 79,
            "thinking_tokens": 66,
            "cache_read_tokens": 8142,
            "total_tokens": 10328,
        },
        "duration_seconds": 1.83,
        "num_turns": 1,
    }
)

CODEX_OUTPUT = "\n".join(
    [
        json.dumps({"type": "thread.started", "thread_id": "thr-123"}),
        json.dumps({"type": "turn.started", "turn_id": "turn-1"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "Task completed successfully.\nRESULT_REF: 4a3b2c1d",
                },
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 15134,
                    "cached_input_tokens": 9984,
                    "cache_write_input_tokens": 0,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 0,
                },
            }
        ),
    ]
)


@pytest.mark.parametrize(
    ("cli", "output", "expected"),
    [
        (
            "claude",
            CLAUDE_OUTPUT,
            {"input_tokens": 2, "output_tokens": 14, "cached_tokens": 15273, "cost_usd": 0.139},
        ),
        (
            "qwen",
            QWEN_OUTPUT,
            {"input_tokens": 49714, "output_tokens": 165, "cached_tokens": 0},
        ),
        (
            "agy",
            AGY_OUTPUT,
            {"input_tokens": 10249, "output_tokens": 79, "cached_tokens": 8142},
        ),
        (
            "codex",
            CODEX_OUTPUT,
            {
                "input_tokens": 15134,
                "output_tokens": 5,
                "cached_tokens": 9984,
                "reasoning_output_tokens": 0,
            },
        ),
    ],
)
def test_parse_cli_token_usage(cli, output, expected):
    assert parse_cli_token_usage(cli, output) == expected


def test_parse_cli_token_usage_accepts_jsonl_result_as_last_object():
    output = '\n'.join(
        [
            json.dumps({"type": "assistant", "message": "working"}),
            CLAUDE_OUTPUT,
        ]
    )

    assert parse_cli_token_usage("claude", output)["cost_usd"] == 0.139


def test_parse_claude_cost_has_same_session_scope_as_total_usage():
    usage = parse_cli_token_usage("claude", CLAUDE_MULTI_MODEL_OUTPUT)

    assert usage == {
        "input_tokens": 224,
        "output_tokens": 69332,
        "cached_tokens": 13659112,
        "cost_usd": pytest.approx(6.175989600000001),
    }


@pytest.mark.parametrize("cli", ["unknown", "invalid_vendor"])
def test_parse_cli_token_usage_fails_closed_for_unsupported_cli(cli):
    assert parse_cli_token_usage(cli, CLAUDE_OUTPUT) is None


def test_parse_cli_token_usage_malformed_output_is_silent():
    assert parse_cli_token_usage("claude", "not json") is None


def test_extract_explicit_result_ref_from_json_result_field():
    output = json.dumps({"type": "result", "result": "done\nRESULT_REF: abc123"})

    assert _extract_explicit_result_ref(output) == "abc123"


def test_record_cli_usage_attributes_run_and_task(db_session):
    project = Project(id="usage-project", name="Usage project")
    task = Task(id="USAGE-001", project=project.id, title="Measure CLI")
    agent = Agent(
        id="@claude",
        name="Claude",
        role="executor",
        cli="claude",
        model="claude-opus-5",
    )
    run = AgentRun(
        id="usage-run-001",
        task_id=task.id,
        agent_id=agent.id,
        cli="claude",
        command="claude",
    )
    db_session.add_all([project, task, agent, run])
    db_session.commit()

    _record_cli_usage(db_session, run, "claude", CLAUDE_OUTPUT)

    usage = db_session.query(LLMUsage).one()
    assert usage.agent_run_id == run.id
    assert usage.task_id == task.id
    assert usage.model == "claude-opus-5"
    assert usage.provider == "claude"
    assert usage.input_tokens == 2
    assert usage.output_tokens == 14
    assert usage.cached_tokens == 15273
    assert float(usage.cost_usd) == pytest.approx(0.139)
    # Claude subscription costUSD is retained as vendor telemetry, but is not
    # an authoritative USD amount and cannot trip the USD safety brake.
    assert float(TaskValidator(db_session)._task_cost(task)) == pytest.approx(0)
    _record_run_resource_usage(db_session, run)
    resource_usage = db_session.query(RunResourceUsage).one()
    assert float(resource_usage.estimated_cost_usd) == pytest.approx(0)


def test_record_cli_usage_codex_attributes_run_and_task(db_session):
    project = Project(id="codex-project", name="Codex project")
    task = Task(id="CODEX-001", project=project.id, title="Measure Codex")
    agent = Agent(
        id="@gpt-5.6-luna",
        name="Luna",
        role="executor",
        cli="codex",
        model="gpt-5.6-luna",
    )
    run = AgentRun(
        id="codex-run-001",
        task_id=task.id,
        agent_id=agent.id,
        cli="codex",
        command="codex exec --json",
    )
    db_session.add_all([project, task, agent, run])
    db_session.commit()

    _record_cli_usage(db_session, run, "codex", CODEX_OUTPUT)

    usage = db_session.query(LLMUsage).one()
    assert usage.agent_run_id == run.id
    assert usage.task_id == task.id
    assert usage.model == "gpt-5.6-luna"
    assert usage.provider == "codex"
    assert usage.input_tokens == 15134
    assert usage.output_tokens == 5
    assert usage.cached_tokens == 9984
    assert float(usage.cost_usd) == 0.0


@pytest.mark.asyncio
async def test_get_stats_does_not_present_unpriced_cli_tokens_as_free(db_session):
    project = Project(id="qwen-project", name="Qwen project")
    task = Task(id="QWEN-001", project=project.id, title="Measure Qwen")
    run = AgentRun(
        id="qwen-run-001",
        task_id=task.id,
        agent_id="@qwen",
        cli="qwen",
        command="qwen",
        status="success",
    )
    db_session.add_all(
        [
            project,
            task,
            run,
            LLMUsage(
                task_id=task.id,
                agent_run_id=run.id,
                model="qwen3.7-plus",
                provider="qwen",
                operation="cli",
                input_tokens=49714,
                output_tokens=165,
                cached_tokens=0,
                cost_usd=0,
            ),
            RunResourceUsage(agent_run_id=run.id, estimated_cost_usd=0),
        ]
    )
    db_session.commit()

    stats = await CommandRouter(db_session).execute_tool(
        "get_stats", {"task_id": task.id}, "usage-session"
    )

    assert stats["cost_status"] == "unmeasured"
    assert stats["input_tokens"] == 49714
    assert stats["output_tokens"] == 165
    assert stats["cost_usd"] is None
    assert "not an authoritative subscription charge" in stats["cost_note"]
    assert stats["cost_scope"] == "recorded_api_usage_only"
