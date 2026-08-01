from __future__ import annotations

import json
import shlex
import os

from app.core.config import settings
from app.db.models import Agent, Project, Task
from app.services.command_builder import build_dispatch_command
from app.services.cli_dispatcher import build_mcp_config


def test_native_mcp_config_uses_streamable_http():
    config = build_mcp_config(
        "http://localhost:8000", "token", native_url="http://localhost:8100/mcp",
        role="executor",
    )
    server = config["mcpServers"]["control-tower"]
    assert server["type"] == "http"
    assert server["url"].endswith("/mcp")
    assert server["headers"]["Authorization"] == "Bearer token"


def test_executor_builder_command_is_pure_cli(monkeypatch, tmp_path):
    """build_dispatch_command must return pure CLI commands without temp files or MCP flags."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")
    task = Task(id="PHASE2-1", project="p", title="Task", acceptance_criteria=["Pass"])
    agent = Agent(id="@executor", name="Executor", role="executor", cli="claude")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, repo_root, cli = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)
    assert "--mcp-config" not in argv
    assert command.startswith("claude ")


def test_codex_executor_builder_command_is_pure_cli(monkeypatch, tmp_path):
    """codex build_dispatch_command is pure CLI without MCP flags."""
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")
    task = Task(id="PHASE2-2", project="p", title="Task", acceptance_criteria=["Pass"])
    agent = Agent(id="@executor", name="Executor", role="executor", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, repo_root, cli = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)
    assert "--mcp-config" not in argv
    assert command.startswith("codex exec")


