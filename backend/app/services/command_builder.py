"""Build shell-safe CLI commands for configured executor agents."""

from __future__ import annotations

import os
import shlex
from typing import Optional

from app.db.models import Agent, Project, Task

SUPPORTED_CLIS = {"agy", "codex", "claude"}


def build_dispatch_command(
    task: Task,
    agent: Agent,
    project: Optional[Project] = None,
) -> tuple[str, str, str]:
    cli = (agent.cli or _infer_cli(agent.model, agent.id)).strip().lower()
    if cli not in SUPPORTED_CLIS:
        raise ValueError(
            f"Agent {agent.id} has unsupported CLI {cli!r}; "
            f"expected one of {sorted(SUPPORTED_CLIS)}"
        )

    repo_root = project.repo_root if project else None
    if not repo_root:
        raise ValueError(f"Project {task.project} does not define repo_root")
    repo_root = os.path.abspath(repo_root)
    if not os.path.isdir(repo_root):
        raise ValueError(f"Project repository does not exist: {repo_root}")

    prompt = _task_prompt(task)
    if cli == "codex":
        argv = ["codex", "exec"]
        if agent.model:
            argv.extend(["-m", agent.model])
        argv.extend(
            [
                "-c",
                f"model_reasoning_effort={agent.effort or 'medium'}",
                "--dangerously-bypass-approvals-and-sandbox",
                prompt,
            ]
        )
    elif cli == "claude":
        argv = ["claude"]
        if agent.model:
            argv.extend(["--model", agent.model])
        argv.extend(["-p", prompt, "--dangerously-skip-permissions"])
    else:
        argv = ["agy"]
        if agent.model:
            argv.extend(["--model", agent.model])
        argv.extend(["--print", prompt, "--dangerously-skip-permissions"])

    return shlex.join(argv), repo_root, cli


def _infer_cli(model: str | None, agent_id: str) -> str:
    lowered = (model or agent_id).lower()
    if "claude" in lowered:
        return "claude"
    if lowered.startswith("gpt-") or "codex" in lowered:
        return "codex"
    return "agy"


def _task_prompt(task: Task) -> str:
    details = task.raw_input or task.title
    sections = [f"Execute task {task.id}: {task.title}", details]
    if task.acceptance_criteria:
        sections.append(
            "Acceptance criteria:\n"
            + "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
        )
    if task.files:
        sections.append("Relevant files:\n" + "\n".join(f"- {path}" for path in task.files))
    if task.tests:
        sections.append("Required tests:\n" + "\n".join(f"- {test}" for test in task.tests))
    if task.plan:
        sections.append(f"Plan:\n{task.plan}")
    sections.append(
        "Complete every acceptance criterion, run the relevant tests, and commit the changes."
    )
    return "\n\n".join(sections)
