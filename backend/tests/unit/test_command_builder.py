import shlex

import pytest

from app.db.models import Agent, Project, Task
from app.services.command_builder import build_dispatch_command


@pytest.mark.parametrize(
    ("cli", "model", "expected_prefix"),
    [
        ("codex", "gpt-5.6-sol", ["codex", "exec", "-m", "gpt-5.6-sol"]),
        ("claude", "claude-opus-4", ["claude", "--model", "claude-opus-4"]),
        ("agy", "gemini-3.6-pro", ["agy", "--model", "gemini-3.6-pro"]),
    ],
)
def test_builds_shell_safe_cli_commands(cli, model, expected_prefix):
    task = Task(
        id="CMD-001",
        project="p",
        title="Do $(touch /tmp/nope)",
        acceptance_criteria=["Pass `all` checks"],
    )
    agent = Agent(
        id="@agent",
        name="Agent",
        role="executor",
        cli=cli,
        model=model,
        effort="high",
    )
    project = Project(id="p", name="Project", repo_root="/tmp")

    command, repo_root, selected_cli = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)

    assert argv[: len(expected_prefix)] == expected_prefix
    assert repo_root == "/tmp"
    assert selected_cli == cli
    assert any("$(touch /tmp/nope)" in value for value in argv)


def test_rejects_project_without_repository():
    task = Task(id="CMD-002", project="p", title="Task")
    agent = Agent(id="@agent", name="Agent", role="executor", cli="codex")

    with pytest.raises(ValueError, match="repo_root"):
        build_dispatch_command(task, agent, Project(id="p", name="Project"))


def test_review_prompt_requires_versioned_json_result(tmp_path):
    task = Task(
        id="REV-001",
        project="p",
        title="Run /code-review",
        acceptance_criteria=["All checks pass"],
    )
    agent = Agent(id="@agent", name="Agent", role="executor", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    prompt = shlex.split(command)[-1]

    assert ".ct/review-REV-001.json" in prompt
    assert "verdict (only \"pass\" or \"fail\")" in prompt
    assert "one item per acceptance criterion" in prompt
