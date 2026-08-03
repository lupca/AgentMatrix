from __future__ import annotations

import json
import os
import shlex
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.db.models import Session
from app.services.cli_dispatcher import (
    CLIDispatcher,
    build_mcp_config,
    format_history_as_prompt,
    build_cli_command,
    write_mcp_config,
)
from app.services.coordinator import CoordinatorService
from app.services.process_manager import ProcessResult, ProcessStatus


def test_prompt_formatter_preserves_system_turns_and_tool_results():
    prompt = format_history_as_prompt(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Find the status."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "name": "status"}],
            },
            {
                "role": "tool",
                "name": "status",
                "tool_call_id": "call-1",
                "content": {"status": "done"},
            },
        ]
    )

    assert prompt.index("SYSTEM:") < prompt.index("USER:")
    assert "TOOL_CALLS:" in prompt
    assert "TOOL (status):" in prompt
    assert '"status": "done"' in prompt


def test_cli_commands_use_account_login_and_shell_safe_prompt():
    assert build_cli_command("claude", "claude-sonnet-4", 'say "hi"').startswith(
        "claude --model claude-sonnet-4 -p"
    )
    agy_command = build_cli_command("agy", "gemini-2.5-pro", "hello world")
    assert "agy --model gemini-2.5-pro --print" in agy_command
    assert "hello world" in agy_command


@pytest.mark.asyncio
async def test_cli_dispatcher_forwards_process_output_and_raises_failures(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        ["first", "second", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    dispatcher = CLIDispatcher(working_directory="/tmp")
    chunks = [
        chunk
        async for chunk in dispatcher.spawn("claude", "claude-sonnet-4", "prompt")
    ]

    assert chunks == ["first\n", "second\n"]
    command, cwd = manager.run_with_streaming.call_args.args
    assert command.startswith("claude --model claude-sonnet-4 --mcp-config")
    assert cwd == "/tmp"
    manager.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_cli_dispatcher_spawn_overrides_process_cwd(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    async for _ in CLIDispatcher(working_directory="/default").spawn(
        "codex", "gpt-5", "research", cwd="/project/repo"
    ):
        pass

    _command, cwd = manager.run_with_streaming.call_args.args
    assert cwd == "/project/repo"


@pytest.mark.asyncio
async def test_cli_dispatcher_restores_markdown_line_boundaries(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        [
            "### Header",
            "",
            "* Item 1",
            "* Item 2",
            ProcessResult(ProcessStatus.COMPLETED, 0, None),
        ]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    chunks = [
        chunk
        async for chunk in CLIDispatcher(working_directory="/tmp").spawn(
            "claude", "claude-sonnet-4", "prompt"
        )
    ]

    assert "".join(chunks) == "### Header\n\n* Item 1\n* Item 2\n"


@pytest.mark.asyncio
async def test_cli_dispatcher_surfaces_process_failure(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.TIMEOUT, -1, "Timeout")]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    with pytest.raises(RuntimeError, match="Timeout"):
        async for _ in CLIDispatcher(working_directory="/tmp").spawn(
            "agy", "gemini-2.5-pro", "prompt"
        ):
            pass


@pytest.mark.asyncio
async def test_cli_coordinator_rehydrates_history_when_switching_models(db_session):
    first_manager = MagicMock()
    first_manager.run_with_streaming.return_value = iter(
        ["Ada remembered", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    second_manager = MagicMock()
    second_manager.run_with_streaming.return_value = iter(
        ["Ada confirmed", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    managers = iter([first_manager, second_manager])
    dispatcher = CLIDispatcher(
        working_directory="/tmp",
        process_manager_factory=lambda **_: next(managers),
    )
    service = CoordinatorService(db_session, dispatcher=dispatcher, retry_base_seconds=0)
    session = Session(id="cli-session", thread_id="cli-session", messages=[])
    db_session.add(session)
    db_session.commit()

    await service.complete_turn(
        session,
        "Remember my name is Ada.",
        model="claude-sonnet-4",
        idempotency_key="cli-turn-1",
    )
    await service.complete_turn(
        session,
        "What is my name?",
        model="gemini-2.5-pro",
        idempotency_key="cli-turn-2",
    )

    second_command = second_manager.run_with_streaming.call_args.args[0]
    # agy uses --model (not --agent) and does not support --mcp-config.
    assert second_command.startswith("agy --model gemini-2.5-pro --print")
    assert "USER:\nRemember my name is Ada." in second_command
    assert "ASSISTANT:\nAda remembered" in second_command
    assert "USER:\nWhat is my name?" in second_command
    assert session.selected_provider == "google"
    assert session.selected_model == "gemini-2.5-pro"


# --- MCP config wiring (ADR-001 §D5, CTV2-084) -----------------------------
# The coordinator chat CLI path only; app/workers/agent_runner.py (executor
# dispatch) never touches CLIDispatcher and is untouched by this feature.


def test_build_cli_command_without_mcp_config_path_is_unchanged():
    assert build_cli_command("claude", "claude-sonnet-4", "hi") == build_cli_command(
        "claude", "claude-sonnet-4", "hi", None
    )


@pytest.mark.parametrize(
    "cli,model,expected_prefix",
    [
        ("claude", "claude-sonnet-4", "claude --model claude-sonnet-4"),
    ],
)
def test_build_cli_command_places_mcp_config_before_the_prompt(cli, model, expected_prefix):
    # --mcp-config is only supported by claude; codex and agy ignore it.
    command = build_cli_command(cli, model, "the prompt text", "/tmp/ct-mcp-x.json")

    assert command.startswith(expected_prefix)
    assert "--mcp-config /tmp/ct-mcp-x.json" in command
    # The prompt stays the final argument regardless of CLI.
    assert command.endswith("'the prompt text'") or command.endswith("the prompt text")
    assert command.index("--mcp-config") < command.rindex("the prompt text")


def test_build_cli_command_agy_omits_mcp_config():
    """agy does not support --mcp-config; the flag must not appear."""
    command = build_cli_command("agy", "gemini-2.5-pro", "hello", "/tmp/ct-mcp-x.json")
    assert "--mcp-config" not in command
    assert "--model gemini-2.5-pro" in command


def test_build_cli_command_codex_omits_mcp_config():
    """codex exec does not support --mcp-config; the flag must not appear."""
    command = build_cli_command("codex", "gpt-5-codex", "hello", "/tmp/ct-mcp-x.json")
    assert "--mcp-config" not in command
    assert "codex exec -m gpt-5-codex" in command


def test_build_mcp_config_registers_the_native_http_server():
    config = build_mcp_config("http://localhost:8000", "scoped-token")

    server = config["mcpServers"]["agmx"]
    assert server["type"] == "http"
    assert server["url"] == "http://localhost:8100/mcp"
    assert server["headers"] == {
        "Authorization": "Bearer scoped-token",
        "X-CT-Role": "coordinator",
    }


def test_write_mcp_config_writes_matching_json_to_disk():
    path = write_mcp_config("http://localhost:8000", "scoped-token")
    try:
        assert os.path.exists(path)
        with open(path) as fh:
            on_disk = json.load(fh)
        assert on_disk == build_mcp_config("http://localhost:8000", "scoped-token")
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_dispatcher_always_injects_native_mcp_config(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        ["ok", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    dispatcher = CLIDispatcher(working_directory="/tmp")
    async for _ in dispatcher.spawn("claude", "claude-sonnet-4", "prompt"):
        pass

    command, _ = manager.run_with_streaming.call_args.args
    assert "--mcp-config" in command


@pytest.mark.asyncio
async def test_dispatcher_injects_mcp_config_when_token_configured(monkeypatch, tmp_path):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        ["ok", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")

    dispatcher = CLIDispatcher(working_directory=str(tmp_path))
    async for _ in dispatcher.spawn("claude", "claude-sonnet-4", "prompt"):
        pass

    command, cwd = manager.run_with_streaming.call_args.args
    assert "--mcp-config" in command
    # Verify temp file was cleaned up after spawn completed
    argv = shlex.split(command)
    config_path = argv[argv.index("--mcp-config") + 1]
    assert not os.path.exists(config_path)


@pytest.mark.asyncio
async def test_dispatcher_reads_mcp_env_defaults(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN_SECRET", "env-secret")

    dispatcher = CLIDispatcher(working_directory="/tmp")

    assert dispatcher.mcp_secret == "env-secret"


def test_cli_commands_forward_configured_effort():
    # agy: gemini-3.6-flash REQUIRES --effort; exits 1 without it.
    agy = build_cli_command("agy", "gemini-3.6-flash", "hi", effort="high")
    assert "--effort high --print hi" in agy
    # model names already carrying an effort suffix must not get the flag twice
    suffixed = build_cli_command("agy", "gemini-3.6-flash-high", "hi", effort="high")
    assert "--effort" not in suffixed
    # no configured effort -> unchanged (some models reject the flag)
    plain = build_cli_command("agy", "gemini-2.5-pro", "hi")
    assert "--effort" not in plain
    claude = build_cli_command("claude", "claude-sonnet-5", "hi", effort="low")
    assert "--effort low" in claude
    codex = build_cli_command("codex", "gpt-5.6-luna", "hi", effort="high")
    assert "model_reasoning_effort=high" in codex
