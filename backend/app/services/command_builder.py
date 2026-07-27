"""Build shell-safe CLI commands for configured executor agents."""

from __future__ import annotations

import os
import shlex
from typing import Optional

from app.db.models import Agent, Project, Task

SUPPORTED_CLIS = {"agy", "codex", "claude"}


def review_result_path(repo_root: str, task_id: str) -> str:
    """Return the stable, ignored path used by a review run's JSON result."""
    safe_task_id = task_id.replace("/", "_").replace("\\", "_")
    return os.path.join(repo_root, ".ct", f"review-{safe_task_id}.json")


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

    prompt = _task_prompt(task, review_result_path(repo_root, task.id))
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


def build_review_command(
    task: Task,
    agent: Agent,
    project: Optional[Project],
    base_ref: str,
    head_ref: str,
) -> tuple[str, str, str]:
    """Build the CLI invocation for a real ``/code-review`` run.

    Unlike :func:`build_dispatch_command`, the diff range is never inferred
    here — it must already be the committed base/head pair CTV2-099 recorded
    on the task (``result_ref``), passed in explicitly by the caller.
    """
    cli = (agent.cli or _infer_cli(agent.model, agent.id)).strip().lower()
    if cli not in SUPPORTED_CLIS:
        raise ValueError(
            f"Agent {agent.id} has unsupported CLI {cli!r}; "
            f"expected one of {sorted(SUPPORTED_CLIS)}"
        )
    if not base_ref or not base_ref.strip():
        raise ValueError("base_ref is required to build a review command")
    if not head_ref or not head_ref.strip():
        raise ValueError("head_ref is required to build a review command")

    repo_root = project.repo_root if project else None
    if not repo_root:
        raise ValueError(f"Project {task.project} does not define repo_root")
    repo_root = os.path.abspath(repo_root)
    if not os.path.isdir(repo_root):
        raise ValueError(f"Project repository does not exist: {repo_root}")

    prompt = _review_prompt(
        task, base_ref, head_ref, review_result_path(repo_root, task.id)
    )
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


def _review_prompt(task: Task, base_ref: str, head_ref: str, result_path: str) -> str:
    sections = [
        f"/code-review --from {base_ref} --to {head_ref}",
        f"Review task {task.id}: {task.title}",
    ]
    if task.acceptance_criteria:
        sections.append(
            "Acceptance criteria:\n"
            + "\n".join(f"- {criterion}" for criterion in task.acceptance_criteria)
        )
    sections.append(
        "Code review result contract:\n"
        f"Write the final review result as JSON to {result_path}. "
        "Do not use stdout as the result. The JSON must contain exactly these "
        "fields: schema_version (\"1.0\"), task_id, base, head, ac_results, "
        "findings, tests_run, tests_passed. Each ac_results item must contain "
        "ac_index, ac_text, verdict (only \"pass\" or \"fail\"), and "
        "evidence (an array of strings). Ensure ac_results has one item per "
        "acceptance criterion."
    )
    return "\n\n".join(sections)


def _infer_cli(model: str | None, agent_id: str) -> str:
    lowered = (model or agent_id).lower()
    if "claude" in lowered:
        return "claude"
    if lowered.startswith("gpt-") or "codex" in lowered:
        return "codex"
    return "agy"


def _task_prompt(task: Task, result_path: str | None = None) -> str:
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
    if _is_review_task(task):
        if result_path is None:
            raise ValueError("result_path is required for a review task")
        sections.append(
            "Code review result contract:\n"
            f"Write the final review result as JSON to {result_path}. "
            "Do not use stdout as the result. The JSON must contain exactly these "
            "fields: schema_version (\"1.0\"), task_id, base, head, ac_results, "
            "findings, tests_run, tests_passed. Each ac_results item must contain "
            "ac_index, ac_text, verdict (only \"pass\" or \"fail\"), and "
            "evidence (an array of strings). Ensure ac_results has one item per "
            "acceptance criterion."
        )
    sections.append(
        "Complete every acceptance criterion, run the relevant tests, and commit the changes. "
        "When done, print the resulting commit hash on its own final line as 'RESULT_REF: <hash>'. "
        "A task with no commit has no result-ref and cannot be reviewed."
    )
    return "\n\n".join(sections)


def _is_review_task(task: Task) -> bool:
    text = " ".join((task.title or "", task.raw_input or "")).lower()
    return "/code-review" in text or "code review" in text
