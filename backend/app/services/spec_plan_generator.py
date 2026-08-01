"""Real spec/plan generation: one LLM call, grounded in research-tool evidence.

Produces `SpecPlanResult` (acceptance_criteria/plan/files/tests/risk) for a
freshly created `Task`. `files` proposed by the LLM that the code graph
cannot confirm are annotated rather than trusted outright, and `flows` come
exclusively from `get_affected_flows` (the LLM never invents flow names).
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.db.models import Agent, Task
from app.schemas.task import SPEC_PLAN_RESULT_SCHEMA_VERSION, SpecPlanResult
from app.services.graph_client import get_affected_flows, semantic_search
from app.services.llm_service import ConfigurationError, LLMService

UNCONFIRMED_SUFFIX = " *(chưa xác nhận)*"
_MAX_ATTEMPTS = 2


class SpecPlanGenerationError(RuntimeError):
    """The LLM did not produce a schema-valid spec/plan after retrying."""


def _build_prompt(
    task: Task,
    graph_candidates: list[str],
    *,
    retry_reason: str | None = None,
    project_context: str | None = None,
) -> str:
    candidates = "\n".join(f"- {c}" for c in graph_candidates) or "(none found)"
    retry_note = (
        f"\nYour previous reply was rejected: {retry_reason}. "
        "Reply again with ONLY the corrected JSON object.\n"
        if retry_reason
        else ""
    )
    # The task description is the single most important planning input — a
    # planner that only sees the title invents plausible-but-wrong work
    # (observed live: VOMA-001).
    details = (task.raw_input or "").strip()
    details_block = f"Task description:\n{details}\n\n" if details else ""
    context_block = (
        f"Project context (conventions and hard boundaries — respect them):\n"
        f"{project_context.strip()}\n\n"
        if project_context and project_context.strip()
        else ""
    )
    return (
        "You are a software spec/plan generator for a task-coordination system.\n"
        f"Task title: {task.title}\n"
        f"Project: {task.project}\n\n"
        f"{details_block}"
        f"{context_block}"
        "Files the code graph reports as relevant to this area (prefer these; "
        "you may name others but they will be marked unconfirmed):\n"
        f"{candidates}\n"
        f"{retry_note}\n"
        "Respond with ONLY a valid JSON object (no markdown fences) with keys:\n"
        f'  "schema_version": "{SPEC_PLAN_RESULT_SCHEMA_VERSION}"\n'
        '  "acceptance_criteria": list of 2-6 criteria. Each MUST be '
        "objectively verifiable by a reviewer (name the observable outcome, "
        "file, or command output — never vague like 'code is clean' or "
        "restating the title)\n"
        '  "plan": one string structured as: a 1-2 sentence intent; "Scope — '
        'in:/out:"; then 4-10 ordered, verb-first, atomic steps '
        "(discovery -> changes -> tests -> verification), each naming likely "
        "files or commands; end with \"Open questions:\" (max 3, or 'none')\n"
        '  "files": list of file paths this task will likely touch\n'
        '  "tests": list of test file paths/commands that verify this task\n'
        '  "risk": one of "low", "medium", "high"\n\n'
        "Base the plan ONLY on the information above — if the description is "
        "too thin to plan responsibly, put what you need to know into Open "
        "questions instead of inventing scope.\n"
    )


def _parse_json(content: str) -> dict:
    text = content.strip()
    # Reasoning models (DeepSeek, GLM, ...) arrive through the OpenAI adapter
    # as '<think>...</think>{json}' — strip the reasoning block first, or
    # json.loads dies at char 0 on perfectly good output.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    # Last resort: models that ignore "ONLY JSON" and add prose around the
    # object. Take the outermost {...} span instead of failing outright.
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


async def generate_spec_plan(
    task: Task,
    repo_root: str | None,
    agent: Agent,
    project_context: str | None = None,
) -> tuple[SpecPlanResult, list[str]]:
    """Call the LLM once (with one retry on schema mismatch) and ground its
    file claims and flows in the code graph. Returns (result, flows).

    ``agent`` is the resolved ``Agent`` to run generation with. There is
    deliberately no environment fallback: callers must resolve an agent
    (e.g. via ``AgentSuggester``) before calling this.
    """
    if agent is None:
        raise ConfigurationError(
            "Spec/plan generation requires an explicitly configured agent."
        )

    graph_candidates: list[str] = []
    if repo_root:
        try:
            found = await semantic_search(
                repo_root, task.title, limit=15, raise_on_error=True
            )
            if isinstance(found, list):
                graph_candidates = [
                    str(item.get("file_path") or item.get("name") or item)
                    for item in found
                    if isinstance(item, dict)
                ]
        except Exception:
            graph_candidates = []

    llm = LLMService()
    retry_reason: str | None = None
    result: SpecPlanResult | None = None
    last_error: Exception | None = None

    for _ in range(_MAX_ATTEMPTS):
        prompt = _build_prompt(
            task,
            graph_candidates,
            retry_reason=retry_reason,
            project_context=project_context,
        )
        response = await llm.complete(
            agent,
            [{"role": "user", "content": prompt}],
            # Reasoning models spend most of the budget on the <think> block
            # before emitting the JSON; 1200 truncated them mid-thought.
            max_tokens=4096,
            temperature=0.3,
        )
        content = response.text
        try:
            parsed = _parse_json(content)
            result = SpecPlanResult.model_validate(parsed)
            last_error = None
            break
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            retry_reason = str(exc)
            continue

    if result is None:
        raise SpecPlanGenerationError(
            f"LLM did not return a schema-valid spec/plan after "
            f"{_MAX_ATTEMPTS} attempts: {last_error}"
        )

    confirmed = set(graph_candidates)
    marked_files = [
        f if f in confirmed else f"{f}{UNCONFIRMED_SUFFIX}" for f in result.files
    ]
    result = result.model_copy(update={"files": marked_files})

    flows: list[str] = []
    real_files = [f for f in result.files if not f.endswith(UNCONFIRMED_SUFFIX)]
    if repo_root and real_files:
        try:
            flows_result = await get_affected_flows(repo_root, real_files)
            if isinstance(flows_result, list):
                flows = flows_result
        except Exception:
            flows = []

    return result, flows
