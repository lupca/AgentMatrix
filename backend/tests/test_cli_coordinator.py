from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

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
    assert "agy --agent gemini-2.5-pro --print" in agy_command
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
    assert command.startswith("claude --model claude-sonnet-4 -p")
    assert cwd == "/tmp"
    manager.terminate.assert_called_once()


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
    assert second_command.startswith("agy --agent gemini-2.5-pro --print")
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
        ("agy", "gemini-2.5-pro", "agy --agent gemini-2.5-pro"),
        ("codex", "gpt-5-codex", "codex exec -m gpt-5-codex"),
    ],
)
def test_build_cli_command_places_mcp_config_before_the_prompt(cli, model, expected_prefix):
    command = build_cli_command(cli, model, "the prompt text", "/tmp/ct-mcp-x.json")

    assert command.startswith(expected_prefix)
    assert "--mcp-config /tmp/ct-mcp-x.json" in command
    # The prompt stays the final argument regardless of CLI.
    assert command.endswith("'the prompt text'") or command.endswith("the prompt text")
    assert command.index("--mcp-config") < command.rindex("the prompt text")


def test_build_mcp_config_registers_the_control_tower_stdio_server():
    config = build_mcp_config("http://localhost:8000", "scoped-token")

    server = config["mcpServers"]["control-tower"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "app.mcp_server", "--api-url", "http://localhost:8000"]
    assert server["env"] == {"CT_MCP_TOKEN": "scoped-token"}


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
async def test_dispatcher_omits_mcp_config_when_no_token_configured(monkeypatch):
    """Default/unconfigured behavior is unchanged from before this feature."""

    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        ["ok", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    dispatcher = CLIDispatcher(working_directory="/tmp")
    assert dispatcher.mcp_token == ""
    async for _ in dispatcher.spawn("claude", "claude-sonnet-4", "prompt"):
        pass

    command, _ = manager.run_with_streaming.call_args.args
    assert "--mcp-config" not in command


@pytest.mark.asyncio
async def test_dispatcher_injects_mcp_config_when_token_configured(monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        ["ok", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager",
        MagicMock(return_value=manager),
    )

    dispatcher = CLIDispatcher(
        working_directory="/tmp",
        api_url="http://localhost:8000",
        mcp_token="scoped-token",
    )
    written_paths: list[str] = []
    real_write_mcp_config = write_mcp_config

    def spy_write_mcp_config(api_url, token):
        path = real_write_mcp_config(api_url, token)
        written_paths.append(path)
        return path

    monkeypatch.setattr(
        "app.services.cli_dispatcher.write_mcp_config", spy_write_mcp_config
    )

    async for _ in dispatcher.spawn("claude", "claude-sonnet-4", "prompt"):
        pass

    command, _ = manager.run_with_streaming.call_args.args
    assert len(written_paths) == 1
    assert f"--mcp-config {written_paths[0]}" in command
    # The dispatcher owns the temp file's lifecycle: gone once the CLI exits.
    assert not os.path.exists(written_paths[0])


@pytest.mark.asyncio
async def test_dispatcher_reads_mcp_env_defaults(monkeypatch):
    monkeypatch.setenv("CT_API_URL", "http://ct-backend:9000")
    monkeypatch.setenv("MCP_API_TOKEN", "env-token")

    dispatcher = CLIDispatcher(working_directory="/tmp")

    assert dispatcher.api_url == "http://ct-backend:9000"
    assert dispatcher.mcp_token == "env-token"
