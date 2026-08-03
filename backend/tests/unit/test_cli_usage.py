import json

import pytest

from app.db.models import Agent, AgentRun, LLMUsage, Project, RunResourceUsage, Task
from app.services.command_router import CommandRouter
from app.services.task_validators import TaskValidator
from app.workers.cli_executor import _record_cli_usage
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


@pytest.mark.parametrize("cli", ["codex", "unknown"])
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
    assert float(TaskValidator(db_session)._task_cost(task)) == pytest.approx(0.139)


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

    assert stats["cost_status"] == "measured"
    assert stats["input_tokens"] == 49714
    assert stats["output_tokens"] == 165
    assert stats["cost_usd"] is None
    assert "no authoritative USD cost" in stats["cost_note"]
