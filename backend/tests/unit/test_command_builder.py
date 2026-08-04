import shlex

import pytest

from app.db.models import Agent, Project, Task
from app.services.command_builder import build_dispatch_command, build_review_command


@pytest.mark.parametrize(
    ("cli", "model", "expected_prefix"),
    [
        ("codex", "gpt-5.6-sol", ["codex", "exec", "--json", "-m", "gpt-5.6-sol"]),
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


@pytest.mark.parametrize("cli", ["claude", "agy"])
def test_dispatch_effort_override_adds_flag(cli):
    task = Task(id="CMD-EFFORT-1", project="p", title="Task", acceptance_criteria=["Pass"])
    agent = Agent(id="@agent", name="Agent", role="executor", cli=cli, effort="low")
    project = Project(id="p", name="Project", repo_root="/tmp")

    command, _, _ = build_dispatch_command(task, agent, project, effort="high")
    argv = shlex.split(command)

    assert "--effort" in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_dispatch_effort_defaults_to_agent_effort_then_medium():
    task = Task(id="CMD-EFFORT-2", project="p", title="Task", acceptance_criteria=["Pass"])
    project = Project(id="p", name="Project", repo_root="/tmp")

    agent_with_effort = Agent(id="@agent", name="Agent", role="executor", cli="claude", effort="low")
    command, _, _ = build_dispatch_command(task, agent_with_effort, project)
    argv = shlex.split(command)
    assert argv[argv.index("--effort") + 1] == "low"

    agent_without_effort = Agent(id="@agent", name="Agent", role="executor", cli="claude")
    command, _, _ = build_dispatch_command(task, agent_without_effort, project)
    argv = shlex.split(command)
    assert argv[argv.index("--effort") + 1] == "medium"


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


def test_task_prompt_includes_findings_on_changes_requested(tmp_path):
    task = Task(
        id="FIX-001",
        project="p",
        title="Fix review findings",
        status="changes-requested",
        verdict="changes",
        findings=[
            {
                "file": "backend/app/example.py",
                "line": 42,
                "severity": "high",
                "description": "Validate the user-controlled path before opening it.",
            }
        ],
        acceptance_criteria=["Tests pass"],
    )
    agent = Agent(id="@agent", name="Agent", role="executor", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    prompt = shlex.split(command)[-1]

    assert "Review feedback to address" in prompt
    assert "you are fixing issues, not starting over" in prompt
    assert "Verdict: changes" in prompt
    assert "file: backend/app/example.py" in prompt
    assert "line: 42" in prompt
    assert "severity: high" in prompt
    assert "Validate the user-controlled path before opening it." in prompt


def test_build_review_command_embeds_explicit_from_to_range(tmp_path):
    task = Task(
        id="REV-002",
        project="p",
        title="Implement feature X",
        acceptance_criteria=["Handles edge case"],
    )
    agent = Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="claude")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, repo_root, cli = build_review_command(
        task, agent, project, "base-sha", "head-sha"
    )
    argv = shlex.split(command)
    prompt = argv[argv.index("-p") + 1]

    assert repo_root == str(tmp_path)
    assert cli == "claude"
    assert prompt.startswith("/code-review --from base-sha --to head-sha")
    assert ".ct/review-REV-002.json" in prompt
    assert "optional toolchain_results (an object)" in prompt
    assert "do NOT add toolchain_output, toolchain_notes, notes" in prompt


@pytest.mark.parametrize("cli", ["claude", "qwen", "agy"])
def test_dispatch_stream_json_output_flags_preserve_prompt_contract(cli, tmp_path):
    task = Task(id="CMD-JSON", project="p", title="Task")
    agent = Agent(id="@agent", name="Agent", role="executor", cli=cli, model="model")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)

    format_index = argv.index("--output-format")
    expected_format = "stream-json"
    assert argv[format_index : format_index + 2] == ["--output-format", expected_format]
    if cli == "agy":
        assert argv[format_index + 2] == "--print"
        assert argv[format_index + 3] == argv[-1]
    elif cli == "claude":
        assert argv[argv.index("-p") + 1] != "--output-format"
        assert argv[format_index - 1] == "--dangerously-skip-permissions"
        assert "--verbose" in argv
    else:
        assert argv[argv.index("-p") + 1] == argv[format_index - 1]
        if cli == "qwen":
            assert argv[argv.index("--yolo") + 1] == "-p"


def test_codex_receives_json_output_flag(tmp_path):
    task = Task(id="CMD-CODEX", project="p", title="Task")
    agent = Agent(id="@agent", name="Agent", role="executor", cli="codex", model="gpt-5")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    argv = shlex.split(command)

    assert argv[:3] == ["codex", "exec", "--json"]


@pytest.mark.parametrize("cli", ["claude", "qwen", "agy", "codex"])
def test_review_command_output_flags_match_cli_contract(cli, tmp_path):
    task = Task(id="REV-JSON", project="p", title="Review task")
    agent = Agent(id="@reviewer", name="Reviewer", role="reviewer", cli=cli)
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_review_command(task, agent, project, "base", "head")
    argv = shlex.split(command)

    if cli == "codex":
        assert argv[:3] == ["codex", "exec", "--json"]
        return

    format_index = argv.index("--output-format")
    expected_format = "stream-json"
    assert argv[format_index : format_index + 2] == ["--output-format", expected_format]
    if cli == "claude":
        assert "--verbose" in argv
    if cli == "agy":
        assert argv[format_index + 2] == "--print"
        assert argv[format_index + 3] == argv[-1]
    else:
        prompt_flag = "-p"
        assert argv[argv.index(prompt_flag) + 1]
        if cli == "qwen":
            assert argv[argv.index("--yolo") + 1] == "-p"


def test_build_review_command_never_infers_a_missing_base_ref(tmp_path):
    task = Task(id="REV-003", project="p", title="Implement feature Y")
    agent = Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    with pytest.raises(ValueError, match="base_ref"):
        build_review_command(task, agent, project, "", "head-sha")


def test_task_prompt_includes_graph_tool_guidance(tmp_path):
    task = Task(id="GRAPH-001", project="p", title="Implement feature with graph tools")
    agent = Agent(id="@agent", name="Agent", role="executor", cli="codex")
    project = Project(id="p", name="Project", repo_root=str(tmp_path))

    command, _, _ = build_dispatch_command(task, agent, project)
    prompt = shlex.split(command)[-1]

    # Verify presence of tools and input formats
    assert "get_impact_radius" in prompt
    assert "get_minimal_context" in prompt
    assert 'get_impact_radius {"file":' in prompt
    assert 'get_minimal_context {"query":' in prompt
    assert '"limit": 10' in prompt

    # Verify main repo vs worktree warning
    assert "main repo" in prompt
    assert "worktree" in prompt

    # Verify load_tools is NOT recommended/mentioned
    assert "load_tools" not in prompt

    # Extract the graph tool section and check character length (< 400 chars)
    graph_section = [
        line for line in prompt.split("\n\n") if "get_impact_radius" in line
    ][0]
    assert len(graph_section) < 400
