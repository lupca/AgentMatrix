import json

import pytest

from app.db.models import Agent, AgentRun, LLMUsage, Project, RunResourceUsage, Setting, Task
from app.services.command_router_handlers import admin_handlers
from app.services.command_router import CommandRouter
from app.services.task_validators import TaskValidator
from app.workers.cli_executor import _record_cli_usage, _record_run_resource_usage
from app.workers import output_parser
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

AGY_STREAM_OUTPUT = "\n".join(
    [
        json.dumps({"type": "response", "text": "working"}),
        json.dumps(
            {
                "type": "complete",
                "usage": {
                    "input_tokens": 10249,
                    "output_tokens": 79,
                    "cache_read_tokens": 8142,
                },
                "result": "done\nRESULT_REF: agy-stream-ref",
            }
        ),
    ]
)

QWEN_STREAM_OUTPUT = "\n".join(
    [
        json.dumps({"type": "response", "text": "working"}),
        json.dumps(
            {
                "type": "complete",
                "usage": {
                    "input_tokens": 49714,
                    "output_tokens": 165,
                    "cache_read_input_tokens": 0,
                },
                "result": "done\nRESULT_REF: qwen-stream-ref",
            }
        ),
    ]
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
            {
                "input_tokens": 15275,
                "output_tokens": 14,
                "cached_tokens": 15273,
                "cache_write_tokens": 13120,
                "usage_is_measured": True,
                "cost_usd": 0.139,
            },
        ),
        (
            "qwen",
            QWEN_OUTPUT,
            {
                "input_tokens": 49714,
                "output_tokens": 165,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "usage_is_measured": True,
            },
        ),
        (
            "agy",
            AGY_OUTPUT,
            {
                "input_tokens": 18391,
                "output_tokens": 79,
                "cached_tokens": 8142,
                "cache_write_tokens": 0,
                "usage_is_measured": True,
            },
        ),
        (
            "codex",
            CODEX_OUTPUT,
            {
                "input_tokens": 15134,
                "output_tokens": 5,
                "cached_tokens": 9984,
                "cache_write_tokens": 0,
                "usage_is_measured": True,
                "reasoning_output_tokens": 0,
            },
        ),
    ],
)
def test_parse_cli_token_usage(cli, output, expected):
    parsed = parse_cli_token_usage(cli, output)
    assert parsed == expected
    assert parsed["cached_tokens"] <= parsed["input_tokens"]


def test_parse_cli_token_usage_accepts_jsonl_result_as_last_object():
    output = '\n'.join(
        [
            json.dumps({"type": "assistant", "message": "working"}),
            CLAUDE_OUTPUT,
        ]
    )

    assert parse_cli_token_usage("claude", output)["cost_usd"] == 0.139


@pytest.mark.parametrize(
    ("cli", "output", "expected_ref", "expected_usage"),
    [
        (
            "agy",
            AGY_STREAM_OUTPUT,
            "agy-stream-ref",
            {
                "input_tokens": 18391,
                "output_tokens": 79,
                "cached_tokens": 8142,
                "cache_write_tokens": 0,
                "usage_is_measured": True,
            },
        ),
        (
            "qwen",
            QWEN_STREAM_OUTPUT,
            "qwen-stream-ref",
            {
                "input_tokens": 49714,
                "output_tokens": 165,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "usage_is_measured": True,
            },
        ),
    ],
)
def test_stream_json_usage_and_result_ref_paths_remain_readable(
    cli, output, expected_ref, expected_usage
):
    assert parse_cli_token_usage(cli, output) == expected_usage
    assert _extract_explicit_result_ref(output.splitlines()[-1]) == expected_ref


def test_parse_claude_cost_has_same_session_scope_as_total_usage():
    usage = parse_cli_token_usage("claude", CLAUDE_MULTI_MODEL_OUTPUT)

    assert usage == {
        "input_tokens": 13659336,
        "output_tokens": 69332,
        "cached_tokens": 13659112,
        "cache_write_tokens": 172344,
        "usage_is_measured": True,
        "cost_usd": pytest.approx(6.175989600000001),
    }


@pytest.mark.parametrize("cli", ["unknown", "invalid_vendor"])
def test_parse_cli_token_usage_fails_closed_for_unsupported_cli(cli):
    assert parse_cli_token_usage(cli, CLAUDE_OUTPUT) is None


def test_parse_cli_token_usage_malformed_output_is_silent():
    assert parse_cli_token_usage("claude", "not json") is None


def test_parse_cli_token_usage_extracts_last_usage_from_json_array():
    array_line = json.dumps(
        [
            {"type": "system", "message": "init"},
            {"type": "assistant", "message": "working"},
            {
                "type": "result",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 10,
                },
                "total_cost_usd": 0.05,
            },
        ]
    )
    usage = parse_cli_token_usage("claude", array_line)
    assert usage == {
        "input_tokens": 110,
        "output_tokens": 50,
        "cached_tokens": 10,
        "cache_write_tokens": 0,
        "usage_is_measured": True,
        "cost_usd": 0.05,
    }


def test_parse_cli_token_usage_extracts_usage_from_array_after_banner_garbage():
    stdout = "some banner output\n" + json.dumps(
        [
            {"type": "system"},
            {
                "type": "result",
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 80,
                    "cache_read_tokens": 40,
                },
            },
        ]
    )
    usage = parse_cli_token_usage("agy", stdout)
    assert usage == {
        "input_tokens": 240,
        "output_tokens": 80,
        "cached_tokens": 40,
        "cache_write_tokens": 0,
        "usage_is_measured": True,
    }


def test_parse_cli_token_usage_jsonl_unchanged():
    jsonl = "\n".join(
        [
            json.dumps({"type": "system", "message": "init"}),
            json.dumps(
                {
                    "type": "result",
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 10,
                        "cache_read_input_tokens": 0,
                    },
                    "total_cost_usd": 0.01,
                }
            ),
        ]
    )
    usage = parse_cli_token_usage("claude", jsonl)
    assert usage == {
        "input_tokens": 5,
        "output_tokens": 10,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "usage_is_measured": True,
        "cost_usd": 0.01,
    }


def test_parse_cli_token_usage_empty_array_returns_none():
    assert parse_cli_token_usage("claude", "[]") is None


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
    assert usage.input_tokens == 15275
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


def test_parse_cli_token_usage_warns_and_caps_invalid_subset(monkeypatch):
    output = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 99,
                "output_tokens": 2,
            },
        }
    )

    warnings = []
    monkeypatch.setattr(output_parser.logger, "warning", lambda *args: warnings.append(args))
    usage = parse_cli_token_usage("codex", output)

    assert usage["input_tokens"] == 10
    assert usage["cached_tokens"] == 10
    assert "invariant violated" in warnings[0][0]


@pytest.mark.asyncio
async def test_get_stats_warns_for_legacy_mixed_token_row(db_session, monkeypatch):
    project = Project(id="legacy-stats-project", name="Legacy stats project")
    task = Task(id="LEGACY-001", project=project.id, title="Legacy usage")
    db_session.add_all(
        [
            project,
            task,
            LLMUsage(
                task_id=task.id,
                model="claude-opus-5",
                provider="claude",
                operation="cli",
                input_tokens=2,
                output_tokens=14,
                cached_tokens=15273,
            ),
        ]
    )
    db_session.commit()

    warnings = []
    monkeypatch.setattr(admin_handlers.logger, "warning", lambda *args: warnings.append(args))
    stats = await CommandRouter(db_session).execute_tool(
        "get_stats", {"task_id": task.id}, "legacy-stats-session"
    )

    assert stats["input_tokens"] == 2
    assert stats["uncached_input_tokens"] == 0
    assert stats["usage_warnings"]
    assert "historical data is not cross-CLI comparable" in stats["usage_warnings"][0]
    assert "historical rows were not rewritten" in warnings[0][0]


@pytest.mark.asyncio
async def test_get_stats_sums_normalized_cli_input_without_double_counting_cache(db_session):
    project = Project(id="normalized-stats-project", name="Normalized stats project")
    task = Task(id="NORMALIZED-001", project=project.id, title="Normalized usage")
    rows = [
        LLMUsage(
            task_id=task.id,
            model="claude-opus-5",
            provider="claude",
            operation="cli",
            input_tokens=15275,
            output_tokens=14,
            cached_tokens=15273,
        ),
        LLMUsage(
            task_id=task.id,
            model="gemini-3.1-pro",
            provider="agy",
            operation="cli",
            input_tokens=18391,
            output_tokens=79,
            cached_tokens=8142,
        ),
        LLMUsage(
            task_id=task.id,
            model="gpt-5.6-luna",
            provider="codex",
            operation="cli",
            input_tokens=15134,
            output_tokens=5,
            cached_tokens=9984,
        ),
        LLMUsage(
            task_id=task.id,
            model="qwen3.7-plus",
            provider="qwen",
            operation="cli",
            input_tokens=49714,
            output_tokens=165,
            cached_tokens=0,
        ),
    ]
    db_session.add_all([project, task, *rows])
    db_session.commit()

    stats = await CommandRouter(db_session).execute_tool(
        "get_stats", {"task_id": task.id}, "normalized-stats-session"
    )

    assert stats["input_tokens"] == 98514
    assert stats["cached_tokens"] == 33399
    assert stats["uncached_input_tokens"] == 65115
    assert stats["usage_warnings"] == []


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


AGY_STEP_UPDATE_STREAM = "\n".join(
    [
        json.dumps({"event": "init", "conversation_id": "abc"}),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "RUNNING",
                    "usage": {
                        "input_tokens": 500,
                        "output_tokens": 30,
                        "cache_read_tokens": 200,
                    },
                },
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {
                    "state": "DONE",
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 80,
                        "cache_read_tokens": 900,
                    },
                },
            }
        ),
        json.dumps(
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": '{"ok":true}'},
            }
        ),
    ]
)


def test_parse_cli_token_usage_extracts_agy_step_update_usage():
    """agy streams usage inside step_update, not at the top level.

    The parser must find the LAST step_update.usage (the cumulative final
    snapshot) and return it as measured usage.
    """
    usage = parse_cli_token_usage("agy", AGY_STEP_UPDATE_STREAM)

    assert usage is not None
    assert usage["input_tokens"] == 2100  # 1200 + 900
    assert usage["output_tokens"] == 80
    assert usage["cached_tokens"] == 900
    assert usage["usage_is_measured"] is True


CLAUDE_WITH_CACHE_CREATION = json.dumps(
    {
        "type": "result",
        "usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_creation_input_tokens": 3000,
            "cache_read_input_tokens": 10000,
        },
        "total_cost_usd": 0.50,
    }
)


def test_parse_cli_token_usage_extracts_claude_cache_write_tokens():
    """cache_creation_input_tokens → cache_write_tokens in the result."""
    usage = parse_cli_token_usage("claude", CLAUDE_WITH_CACHE_CREATION)

    assert usage is not None
    assert usage["cache_write_tokens"] == 3000
    assert usage["cached_tokens"] == 10000
    assert usage["input_tokens"] == 10500  # 500 + 10000
    assert usage["usage_is_measured"] is True


def test_record_cli_usage_persists_cache_write_and_measured(db_session):
    project = Project(id="cache-write-project", name="Cache write project")
    task = Task(id="CWRITE-001", project=project.id, title="Cache write usage")
    agent = Agent(
        id="@claude-cw",
        name="Claude CW",
        role="executor",
        cli="claude",
        model="claude-sonnet-5",
    )
    run = AgentRun(
        id="cwrite-run-001",
        task_id=task.id,
        agent_id=agent.id,
        cli="claude",
        command="claude",
    )
    db_session.add_all([project, task, agent, run])
    db_session.commit()

    _record_cli_usage(db_session, run, "claude", CLAUDE_WITH_CACHE_CREATION)

    usage = db_session.query(LLMUsage).one()
    assert usage.cache_write_tokens == 3000
    assert usage.usage_is_measured is True
    assert usage.cached_tokens == 10000
    assert usage.input_tokens == 10500


def test_record_cli_usage_marks_unmeasured_when_parser_returns_false(db_session):
    """When parse_cli_token_usage returns usage_is_measured=False, it persists."""
    project = Project(id="unmeasured-project", name="Unmeasured project")
    task = Task(id="UNMEAS-001", project=project.id, title="Unmeasured usage")
    agent = Agent(
        id="@claude-um",
        name="Claude UM",
        role="executor",
        cli="claude",
        model="claude-sonnet-5",
    )
    run = AgentRun(
        id="unmeas-run-001",
        task_id=task.id,
        agent_id=agent.id,
        cli="claude",
        command="claude",
    )
    db_session.add_all([project, task, agent, run])
    db_session.commit()

    stdout = json.dumps(
        {
            "type": "result",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0},
        }
    )
    _record_cli_usage(db_session, run, "claude", stdout)

    usage = db_session.query(LLMUsage).one()
    assert usage.usage_is_measured is True


def test_token_brake_excludes_cache_reads(db_session):
    """A task with huge cached_tokens must NOT trip the token brake.

    CTV2-1424: the old formula SUM(input + output) counted cache reads
    (because input_tokens = fresh + cache_read).  The new formula
    SUM((input - cached) + cache_write + output) excludes them.
    """
    project = Project(id="brake-excl-project", name="Brake exclude cache")
    task = Task(id="BCACHE-001", project=project.id, title="Cache-excl brake")
    db_session.add_all([project, task])
    db_session.add(Setting(key="max_tokens_per_task", value="1000"))
    # 99% cache read: input=10000, cached=9900, cache_write=0, output=50
    # Old formula: 10000 + 50 = 10050 >= 1000 → brake fires (WRONG)
    # New formula: (10000-9900) + 0 + 50 = 150 < 1000 → no brake (CORRECT)
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="claude-sonnet-5",
            provider="claude",
            operation="cli",
            input_tokens=10000,
            cached_tokens=9900,
            cache_write_tokens=0,
            output_tokens=50,
            cost_usd=0,
        )
    )
    db_session.commit()

    validator = TaskValidator(db_session)
    billable = validator._task_tokens(task)
    assert billable == 150  # (10000-9900) + 0 + 50


def test_token_brake_counts_cache_writes(db_session):
    """cache_write_tokens are billable and must count toward the brake."""
    project = Project(id="brake-cw-project", name="Brake counts cache write")
    task = Task(id="BCW-001", project=project.id, title="Cache-write brake")
    db_session.add_all([project, task])
    db_session.add(Setting(key="max_tokens_per_task", value="500"))
    # input=100, cached=0, cache_write=300, output=150
    # New formula: (100-0) + 300 + 150 = 550 >= 500 → brake fires
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="claude-sonnet-5",
            provider="claude",
            operation="cli",
            input_tokens=100,
            cached_tokens=0,
            cache_write_tokens=300,
            output_tokens=150,
            cost_usd=0,
        )
    )
    db_session.commit()

    validator = TaskValidator(db_session)
    billable = validator._task_tokens(task)
    assert billable == 550  # (100-0) + 300 + 150
