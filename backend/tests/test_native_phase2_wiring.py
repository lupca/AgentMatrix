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


def test_executor_command_gets_task_scoped_native_token(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setattr(settings, "MCP_NATIVE_URL", "http://localhost:8100/mcp")
    task = Task(id="PHASE2-1", project="p", title="Task", acceptance_criteria=["Pass"])
    agent = Agent(id="@executor", name="Executor", role="executor", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)
    assert "--mcp-config" in argv
    config_path = argv[argv.index("--mcp-config") + 1]
    with open(config_path, encoding="utf-8") as config_file:
        payload = json.load(config_file)
    assert payload["mcpServers"]["control-tower"]["type"] == "http"
    os.unlink(config_path)
