"""Build shell-safe CLI commands for configured executor agents."""

from __future__ import annotations

import os
import json
import shlex
import tempfile
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import Agent, Project, Task
from app.core.config import settings
from app.services.cli_dispatcher import build_mcp_config

SUPPORTED_CLIS = {"agy", "codex", "claude", "qwen"}
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-extra-high", "-max", "-ultra")


def _normalize_acceptance_criteria(ac: list | str | None) -> list[str]:
    """Convert acceptance_criteria to a list, handling string format."""
    if ac is None:
        return []
    if isinstance(ac, list):
        return ac
    # String format: "AC1: ...\nAC2: ..." - split by newline
    return [line.strip() for line in ac.split("\n") if line.strip()]


def _model_has_effort_suffix(model: str | None) -> bool:
    """Check if model name already includes effort level (e.g. gemini-3.6-flash-low)."""
    if not model:
        return False
    lowered = model.lower()
    return any(lowered.endswith(suffix) for suffix in _EFFORT_SUFFIXES)


def review_result_path(repo_root: str, task_id: str) -> str:
    """Return the stable, ignored path used by a review run's JSON result."""
    safe_task_id = task_id.replace("/", "_").replace("\\", "_")
    return os.path.join(repo_root, ".ct", f"review-{safe_task_id}.json")


def build_dispatch_command(
    task: Task,
    agent: Agent,
    project: Optional[Project] = None,
    effort: Optional[str] = None,
    db: Optional[Session] = None,
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

    resolved_effort = effort or agent.effort or "medium"
    model_has_effort = _model_has_effort_suffix(agent.model)
    prompt = _task_prompt(task, review_result_path(repo_root, task.id))
    prompt = _inject_project_context(prompt, project, task, db)
    if cli == "codex":
        argv = ["codex", "exec"]
        if agent.model:
            argv.extend(["-m", agent.model])
        if not model_has_effort:
            argv.extend(["-c", f"model_reasoning_effort={resolved_effort}"])
        argv.extend(["--dangerously-bypass-approvals-and-sandbox", prompt])
    elif cli == "claude":
        argv = ["claude"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if not model_has_effort:
            argv.extend(["--effort", resolved_effort])
        argv.extend(
            [
                "-p",
                prompt,
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
            ]
        )
    elif cli == "qwen":
        # qwen uses -m for model, -p for prompt (similar to claude)
        argv = ["qwen"]
        if agent.model:
            argv.extend(["-m", agent.model])
        argv.extend(["-p", prompt, "--output-format", "json"])
    else:
        # agy: the prompt must directly follow --print — another flag in
        # between makes agy drop the prompt and answer about the flag
        # instead (verified against agy 1.1.9).
        argv = ["agy"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if not model_has_effort:
            argv.extend(["--effort", resolved_effort])
        argv.extend(
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print",
                prompt,
            ]
        )

    return shlex.join(argv), repo_root, cli


def build_review_command(
    task: Task,
    agent: Agent,
    project: Optional[Project],
    base_ref: str,
    head_ref: str,
    db: Optional[Session] = None,
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
    prompt = _inject_project_context(prompt, project, task, db)
    resolved_effort = agent.effort or "medium"
    model_has_effort = _model_has_effort_suffix(agent.model)
    if cli == "codex":
        argv = ["codex", "exec"]
        if agent.model:
            argv.extend(["-m", agent.model])
        if not model_has_effort:
            argv.extend(["-c", f"model_reasoning_effort={resolved_effort}"])
        argv.extend(["--dangerously-bypass-approvals-and-sandbox", prompt])
    elif cli == "claude":
        argv = ["claude"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if not model_has_effort:
            argv.extend(["--effort", resolved_effort])
        argv.extend(
            [
                "-p",
                prompt,
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
            ]
        )
    elif cli == "qwen":
        # qwen uses -m for model, -p for prompt (similar to claude)
        argv = ["qwen"]
        if agent.model:
            argv.extend(["-m", agent.model])
        argv.extend(["-p", prompt, "--output-format", "json"])
    else:
        # agy: the prompt must directly follow --print (see build_dispatch_command).
        argv = ["agy"]
        if agent.model:
            argv.extend(["--model", agent.model])
        if not model_has_effort:
            argv.extend(["--effort", resolved_effort])
        argv.extend(
            [
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print",
                prompt,
            ]
        )

    return shlex.join(argv), repo_root, cli


def _review_prompt(task: Task, base_ref: str, head_ref: str, result_path: str) -> str:
    sections = [
        f"/code-review --from {base_ref} --to {head_ref}",
        f"Review task {task.id}: {task.title}",
        # ADR-009 review toolchain: extra analyzers are opportunistic — a
        # missing binary must never block the review (chỉ ghi nhận và bỏ qua).
        "Review toolchain:\n"
        "If this repo contains .claude/review-toolchain.md, run each tool it "
        f"lists against the range {base_ref}..{head_ref} (e.g. `ocr review "
        f"--from {base_ref} --to {head_ref} --format json`) and fold their "
        "findings into your own, mapped to AC items where relevant. A tool "
        "that is not installed or exits with an error is NOT a review "
        "failure: note it in evidence and continue without it.",
    ]
    ac_list = _normalize_acceptance_criteria(task.acceptance_criteria)
    if ac_list:
        sections.append(
            "Acceptance criteria:\n"
            + "\n".join(f"- {criterion}" for criterion in ac_list)
        )
    template_path = result_path.replace(".json", ".template.json")
    ac_count = len(ac_list)
    sections.append(
        "Code review result contract:\n"
        f"A template file has been generated at {template_path} with exactly "
        f"{ac_count} ac_results slots matching the {ac_count} acceptance criteria. "
        f"Read the template, fill in the values, and write the final result to {result_path}. "
        "Replace FILL_* placeholders with actual values. For each ac_results item: "
        "set status to \"pass\" or \"fail\", fill evidence array with strings explaining "
        "your verdict, and list any finding_ids that apply. Add findings array with any "
        "issues found (each needs id, severity, category, file, line, description). "
        "Fill tests_run and tests_passed arrays with test commands you ran. "
        "Update base and head with the actual refs. "
        "The only allowed top-level keys are schema_version, task_id, base, head, "
        "ac_results, findings, tests_run, tests_passed, and optional "
        "toolchain_results (an object). Put tool notes inside toolchain_results; "
        "do NOT add toolchain_output, toolchain_notes, notes, or any other "
        "top-level key. "
        "Do NOT add or remove ac_results items — the count must stay exactly {ac_count}. "
        "Reviewer execution is read-only: do not create commits or alter refs.".format(
            ac_count=ac_count
        )
    )
    return "\n\n".join(sections)


def _inject_project_context(
    prompt: str,
    project: Optional[Project],
    task: Task,
    db: Optional[Session],
) -> str:
    """Prepend project context/rules sections to ``prompt`` when available.

    Purely opportunistic and additive: a project with no context_md and no
    matching rules leaves ``prompt`` byte-identical.
    """
    sections: list[str] = []

    if project is not None and project.context_md:
        sections.append(f"[Project Context]\n{project.context_md}")

    if db is not None and project is not None:
        from app.services.context_generator import get_matching_rules

        matching_rules = get_matching_rules(db, project.id, task.files)
        if matching_rules:
            rules_text = "\n\n".join(
                f"## {rule.name}\n{rule.content}" for rule in matching_rules
            )
            sections.append(f"[Project Rules]\n{rules_text}")

    if not sections:
        return prompt

    return "\n\n".join(sections + [prompt])


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
    if task.status == "changes-requested":
        sections.append(_review_feedback(task))
    sections.append(
        "Code graph tools reflect the main repo, not your worktree "
        "(use to understand code before editing, not to check your own changes):\n"
        '- get_impact_radius {"file": "<project-relative-path>"}\n'
        '- get_minimal_context {"query": "<search-text>", "limit": 10}'
    )
    sections.append(
        "Execution boundaries:\n"
        "- Perform git operations ONLY inside your assigned worktree. NEVER checkout, reset, stash, or switch branches in the main repository.\n"
        "- NEVER send process signals (e.g. SIGTERM/kill) or restart backend/workers."
    )
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
            "findings, tests_run, tests_passed. tests_run and tests_passed are "
        "arrays of strings — the exact test commands you ran and the subset "
        "that passed (empty arrays if none). Each ac_results item must contain "
            "criterion_id, status (only \"pass\" or \"fail\"), verdict (only "
            "\"pass\" or \"fail\") as legacy alias, evidence (an "
            "array of strings), and finding_ids (an array of strings). Legacy "
            "ac_index/ac_text metadata is optional; verdict is the legacy name "
            "for status. Each finding must contain id, "
            "severity, category, file, line, and description. Ensure ac_results "
            "has one item per acceptance criterion. Reviewer execution is "
            "read-only: do not create commits or alter refs."
        )
    tags = [str(tag).lower() for tag in (task.tags or [])]
    if "no-commit" in tags:
        sections.append(
            "This is a READ-ONLY task (tag: no-commit): do the analysis/"
            "research work, do NOT commit anything, and when done print "
            "exactly 'RESULT_REF: none' on its own final line."
        )
    else:
        sections.append(
            "Complete every acceptance criterion, run the relevant tests, and commit the changes. "
            "When done, print the resulting commit hash on its own final line as 'RESULT_REF: <hash>'. "
            "A task with no commit has no result-ref and cannot be reviewed."
        )
    return "\n\n".join(sections)


def _review_feedback(task: Task) -> str:
    """Render reviewer feedback as a compact, human-readable task section."""
    lines = [
        "Review feedback to address — you are fixing issues, not starting over",
        f"Verdict: {task.verdict or 'changes'}",
    ]
    findings = task.findings or []
    if not findings:
        lines.append("No structured findings were provided; re-check the review and tests.")
        return "\n".join(lines)

    for index, finding in enumerate(findings, start=1):
        if isinstance(finding, dict):
            file_name = finding.get("file") or "unknown file"
            line = finding.get("line") or "unknown line"
            severity = finding.get("severity") or "unspecified"
            description = finding.get("description") or finding.get("message") or "No description"
        else:
            file_name = "unknown file"
            line = "unknown line"
            severity = "unspecified"
            description = str(finding)
        lines.extend(
            [
                f"Finding {index}:",
                f"  file: {file_name}",
                f"  line: {line}",
                f"  severity: {severity}",
                f"  description: {description}",
            ]
        )
    return "\n".join(lines)


def _is_review_task(task: Task) -> bool:
    text = " ".join((task.title or "", task.raw_input or "")).lower()
    return "/code-review" in text or "code review" in text
