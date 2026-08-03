from __future__ import annotations

import json
import os
import shlex
import subprocess
import pytest

from app.core.config import settings
from app.mcp_native import authenticate_token
from app.services.mcp_attach import attach_mcp, detach_mcp, _ensure_git_exclude


def _decode(token: str):
    claims = authenticate_token(token, secret="test-secret")
    assert claims is not None
    return claims


def test_attach_mcp_claude(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    command = "claude --model claude-3-5-sonnet -p 'hello world'"
    final_cmd, env, cleanup = attach_mcp("claude", command, str(tmp_path), task_id="t1")

    assert "--mcp-config" in final_cmd
    argv = shlex.split(final_cmd)
    config_path = argv[argv.index("--mcp-config") + 1]
    assert os.path.exists(config_path)
    assert cleanup == [config_path]

    with open(config_path, encoding="utf-8") as f:
        data = json.load(f)
    server = data["mcpServers"]["agmx"]
    assert server["type"] == "http"
    assert server["url"] == "http://localhost:8100/mcp"
    assert "Authorization" in server["headers"]

    # Clean up
    for p in cleanup:
        if os.path.exists(p):
            os.unlink(p)


def test_attach_mcp_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    command = "codex exec -m gpt-4o 'hello world'"
    final_cmd, env, cleanup = attach_mcp("codex", command, str(tmp_path), task_id="t2")

    assert cleanup == []
    assert "CT_MCP_TOKEN" in env
    token = env["CT_MCP_TOKEN"]

    # Token MUST NOT appear anywhere in the command string (security requirement)
    assert token not in final_cmd

    # Assert -c flags exist
    assert "-c mcp_servers.agmx.url=http://localhost:8100/mcp" in final_cmd
    assert "-c mcp_servers.agmx.bearer_token_env_var=CT_MCP_TOKEN" in final_cmd


def test_attach_mcp_agy(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    command = "agy --model gemini-2.5-pro --print 'hello world'"
    final_cmd, env, cleanup = attach_mcp("agy", command, str(tmp_path), task_id="t3")

    # Command string MUST NOT have any MCP flags added
    assert final_cmd == command
    assert len(cleanup) == 1
    mcp_file = cleanup[0]
    assert mcp_file == os.path.join(str(tmp_path), ".agents", "mcp_config.json")
    assert os.path.exists(mcp_file)

    with open(mcp_file, encoding="utf-8") as f:
        data = json.load(f)

    server = data["mcpServers"]["agmx"]
    # CRITICAL: agy schema MUST use serverUrl, NOT url
    assert "serverUrl" in server
    assert server["serverUrl"] == "http://localhost:8100/mcp"
    assert "url" not in server
    assert "Authorization" in server["headers"]

    # Clean up
    for p in cleanup:
        if os.path.exists(p):
            os.unlink(p)


def test_ensure_git_exclude(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    _ensure_git_exclude(str(tmp_path), ".agents")

    exclude_file = tmp_path / ".git" / "info" / "exclude"
    assert exclude_file.exists()
    content = exclude_file.read_text(encoding="utf-8")
    assert ".agents" in content


def test_executor_token_carries_role_and_task_scope(monkeypatch, tmp_path):
    """Regression: config-shape tests alone let a wrong-role token ship."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    _, env, cleanup = attach_mcp(
        "codex", "codex exec -m gpt-4o hi", str(tmp_path), task_id="T-9"
    )
    claims = _decode(env["CT_MCP_TOKEN"])
    assert claims.role == "executor"
    assert claims.task_id == "T-9"
    detach_mcp(cleanup)


def test_coordinator_token_has_coordinator_role_and_no_task_scope(monkeypatch, tmp_path):
    """Regression: the coordinator path once issued executor tokens scoped to
    a phantom task 'coordinator', locking the coordinator out of every
    coordinator-only tool."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    _, env, cleanup = attach_mcp(
        "codex", "codex exec -m gpt-4o hi", str(tmp_path), role="coordinator"
    )
    claims = _decode(env["CT_MCP_TOKEN"])
    assert claims.role == "coordinator"
    assert claims.task_id is None
    detach_mcp(cleanup)


def test_cli_dispatcher_requests_coordinator_role(monkeypatch):
    """The dispatcher must ask attach_mcp for a coordinator token."""
    from unittest.mock import MagicMock

    from app.services.cli_dispatcher import CLIDispatcher
    from app.services.process_manager import ProcessResult, ProcessStatus

    captured: dict = {}

    def fake_attach(cli, command, workdir, **kwargs):
        captured.update(kwargs, cli=cli)
        return command, {}, []

    monkeypatch.setattr("app.services.mcp_attach.attach_mcp", fake_attach)
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.COMPLETED, 0, None)]
    )
    monkeypatch.setattr(
        "app.services.cli_dispatcher.ProcessManager", MagicMock(return_value=manager)
    )

    import asyncio

    async def run():
        dispatcher = CLIDispatcher(working_directory="/tmp", mcp_token="s")
        async for _ in dispatcher.spawn("claude", "claude-sonnet-4", "hi"):
            pass

    asyncio.run(run())

    assert captured.get("role") == "coordinator"
    assert captured.get("task_id") in (None,)  # no phantom task scope


def test_agy_exclude_effective_in_real_worktree(monkeypatch, tmp_path):
    """Regression: exclude written to .git/worktrees/<n>/info/exclude is
    ignored by git — it must land in the COMMON dir's info/exclude."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    worktree = tmp_path / "wt1"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-q", str(worktree)], check=True
    )

    _, _, cleanup = attach_mcp(
        "agy", "agy --model g --print hi", str(worktree), task_id="T-1"
    )

    status = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".agents" not in status, f"token file visible to git: {status}"
    detach_mcp(cleanup)


def test_agy_merges_and_restores_preexisting_user_config(monkeypatch, tmp_path):
    """Regression: attach used to overwrite a user's own mcp_config.json and
    cleanup then deleted it outright."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    original = {"mcpServers": {"figma": {"serverUrl": "https://figma.example/mcp"}}}
    config_file = agents_dir / "mcp_config.json"
    config_file.write_text(json.dumps(original), encoding="utf-8")

    _, _, cleanup = attach_mcp(
        "agy", "agy --model g --print hi", str(tmp_path), task_id="T-1"
    )

    merged = json.loads(config_file.read_text(encoding="utf-8"))
    assert "figma" in merged["mcpServers"], "user's server must survive the merge"
    assert "agmx" in merged["mcpServers"]

    detach_mcp(cleanup)
    restored = json.loads(config_file.read_text(encoding="utf-8"))
    assert restored == original, "detach must restore the user's original file"
    assert not (agents_dir / "mcp_config.json.ct-orig").exists()


def test_detach_removes_fresh_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")

    _, _, cleanup = attach_mcp(
        "agy", "agy --model g --print hi", str(tmp_path), task_id="T-1"
    )
    assert all(os.path.exists(p) for p in cleanup)
    detach_mcp(cleanup)
    assert not any(os.path.exists(p) for p in cleanup)
