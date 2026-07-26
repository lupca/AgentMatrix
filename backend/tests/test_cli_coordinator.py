from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.db.models import Session
from app.services.cli_dispatcher import (
    CLIDispatcher,
    format_history_as_prompt,
    build_cli_command,
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

    assert chunks == ["first", "second"]
    command, cwd = manager.run_with_streaming.call_args.args
    assert command.startswith("claude --model claude-sonnet-4 -p")
    assert cwd == "/tmp"
    manager.terminate.assert_called_once()


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
