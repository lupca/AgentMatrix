"""Real spec/plan generation: one LLM call, grounded in research-tool evidence.

Produces `SpecPlanResult` (positive outcomes, negative boundaries, reproducible
evidence, prior art, rejected alternatives, and enforced limits) for a
freshly created `Task`. `files` proposed by the LLM that the code graph
cannot confirm are annotated rather than trusted outright, and `flows` come
exclusively from `get_affected_flows` (the LLM never invents flow names).
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.db.models import Agent, Task
from app.schemas.task import (
    PLAN_CRITIC_RESULT_SCHEMA_VERSION,
    SPEC_PLAN_RESULT_SCHEMA_VERSION,
    PlanCriticResult,
    SpecPlanResult,
)
from app.services.graph_client import get_affected_flows, semantic_search
from app.services.llm_service import ConfigurationError, LLMService

UNCONFIRMED_SUFFIX = " *(chưa xác nhận)*"
_MAX_ATTEMPTS = 2
PLAN_CRITIC_TOKEN_BUDGET = 50_000
_PLAN_CRITIC_MAX_OUTPUT_TOKENS = 4_096


class SpecPlanGenerationError(RuntimeError):
    """The LLM did not produce a schema-valid spec/plan after retrying."""


class PlanCriticError(RuntimeError):
    """The independent critic could not produce a valid in-budget verdict."""


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
        "Bạn đang đứng TRONG repo của project. Hãy ĐỌC (read-only, không sửa gì) "
        "các file liên quan tới task — bắt đầu từ README/docs/entry points rồi "
        "lần theo — TRƯỚC KHI viết plan. Dựa trên những gì đã đọc: nếu spec còn "
        "mơ hồ (auth dùng gì, liên kết module nào, convention nào...) thì đặt câu "
        "hỏi cụ thể vào open_questions và chấm spec_clarity tương ứng.\n\n"
        "Files the code graph reports as relevant to this area (prefer these; "
        "you may name others but they will be marked unconfirmed):\n"
        f"{candidates}\n"
        f"{retry_note}\n"
        "Respond with ONLY a valid JSON object (no markdown fences) with keys:\n"
        f'  "schema_version": "{SPEC_PLAN_RESULT_SCHEMA_VERSION}"\n'
        '  "acceptance_criteria": list of positive outcomes that MUST be achieved. '
        "Each MUST be "
        "objectively verifiable by a reviewer (name the observable outcome, "
        "file, or command output — never vague like 'code is clean' or "
        "restating the title)\n"
        '  "constraints": list of invariants that MUST NOT be violated: scope '
        "boundaries (do not touch), form invariants (preserve), and prohibitions "
        "(never do). Keep these separate from positive outcomes.\n"
        '  "evidence": non-empty list of verified facts. EVERY item must have '
        'exactly {"fact": string, "source_type": "command"|"file"|"query", '
        '"source": string, "result": string}. The source MUST be reproducible: '
        "the exact command plus its observed output, an exact file:line plus the "
        "observed content, or the exact query plus its result. Never cite an "
        "unverified assumption.\n"
        '  "prior_art": list of parts already implemented or otherwise solved; '
        "use an empty list only after checking\n"
        '  "ruled_out": list of alternatives already tried or considered, each '
        'as {"approach": string, "reason": string}\n'
        '  "limits": null for low/medium risk when no task-local ceiling is needed, '
        'otherwise {"max_execution_rounds": integer >= 1, "max_tokens": integer >= 1, '
        '"max_cost_usd": number >= 0 or null}. It is REQUIRED for high risk.\n'
        '  "plan": one string structured as: a 1-2 sentence intent; "Scope — '
        'in:/out:"; then 4-10 ordered, verb-first, atomic steps '
        "(discovery -> changes -> tests -> verification), each naming likely "
        "files or commands; end with \"Open questions:\" (max 3, or 'none')\n"
        '  "files": list of file paths this task will likely touch\n'
        '  "tests": list of test file paths/commands that verify this task\n'
        '  "risk": one of "low", "medium", "high"\n'
        '  "spec_clarity": one of "high", "medium", "low"\n'
        '  "open_questions": list of specific unanswered questions (empty only '
        'when the researched spec is clear enough to execute)\n\n'
        "Base the plan on the task input, project context, graph hints, and the "
        "repository source you read. Ask questions after reading instead of "
        "inventing scope.\n"
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
    agent_type = getattr(getattr(agent, "agent_type", None), "value", None) or getattr(
        agent, "agent_type", ""
    )
    if str(agent_type).strip().lower() == "api":
        raise ConfigurationError(
            "Spec/plan research requires a CLI agent that can read the repository; "
            f"{getattr(agent, 'id', '<unknown>')} is API-backed."
        )
    if not repo_root:
        raise ConfigurationError(
            "Spec/plan research requires a configured project repo_root for the CLI agent."
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
            cwd=repo_root,
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


def _estimate_tokens(text: str) -> int:
    """Conservative provider-independent token estimate for the hard critic cap."""

    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def _build_critic_prompt(
    task: Task,
    plan: SpecPlanResult,
    *,
    project_context: str | None = None,
    retry_reason: str | None = None,
) -> str:
    details = (task.raw_input or "").strip()
    context = (project_context or "").strip()
    retry_note = (
        f"\nPrevious critic output was invalid: {retry_reason}. Return corrected JSON only.\n"
        if retry_reason
        else ""
    )
    return (
        "You are the independent PLAN critic in a four-eyes workflow.\n"
        f"Task {task.id}: {task.title}\n"
        f"Task description:\n{details or '(none)'}\n\n"
        f"Project context:\n{context or '(none)'}\n\n"
        "Plan JSON to challenge:\n"
        f"{plan.model_dump_json()}\n"
        f"{retry_note}\n"
        f"HARD TOTAL BUDGET: at most {PLAN_CRITIC_TOKEN_BUDGET} tokens for this critic "
        "run. Stay focused. You are NOT given a diff and MUST NOT run git diff, "
        "git show, broad repository scans, or open unrelated files. Verify only the "
        "specific commands, query results, and file:line citations named in evidence; "
        "you may query spec_item/spec_task_link and use targeted git history only to "
        "check claimed prior art.\n\n"
        "Challenge whether evidence reproduces, prior_art avoids duplicate work, "
        "ruled_out considered credible alternatives, constraints missed an invariant, "
        "and high-risk limits are sufficient. Reject only for a concrete blocking "
        "problem. EVERY rejection finding MUST cite reproducible evidence from a "
        "command+output, file:line, or query+result. If you cannot cite evidence, you "
        "MUST NOT reject.\n\n"
        "Return ONLY JSON with exactly:\n"
        f'  "schema_version": "{PLAN_CRITIC_RESULT_SCHEMA_VERSION}"\n'
        '  "verdict": "accept" or "reject"\n'
        '  "findings": list of {"target": "evidence"|"prior_art"|"ruled_out"|'
        '"constraints"|"limits"|"contract", "description": string, '
        '"evidence": non-empty list of reproducible citations}\n'
        '  "summary": string\n'
    )


async def criticize_spec_plan(
    task: Task,
    plan: SpecPlanResult,
    repo_root: str | None,
    planner_agent: Agent,
    critic_agent: Agent,
    project_context: str | None = None,
) -> tuple[PlanCriticResult, int]:
    """Run one independent, focused critic after planning and before dispatch."""

    if critic_agent is None:
        raise ConfigurationError("Plan criticism requires an explicitly configured critic.")
    planner_id = str(getattr(planner_agent, "id", "") or "").strip().casefold()
    critic_id = str(getattr(critic_agent, "id", "") or "").strip().casefold()
    if not planner_id or not critic_id or planner_id == critic_id:
        raise ConfigurationError("Plan critic must differ from the planner (four-eyes).")
    critic_type = getattr(getattr(critic_agent, "agent_type", None), "value", None) or getattr(
        critic_agent, "agent_type", ""
    )
    if str(critic_type).strip().lower() == "api":
        raise ConfigurationError(
            "Plan criticism requires a CLI agent that can reproduce cited repository evidence."
        )
    if not repo_root:
        raise ConfigurationError("Plan criticism requires a configured project repo_root.")

    llm = LLMService()
    retry_reason: str | None = None
    last_error: Exception | None = None
    spent_tokens = 0
    for _ in range(_MAX_ATTEMPTS):
        prompt = _build_critic_prompt(
            task,
            plan,
            project_context=project_context,
            retry_reason=retry_reason,
        )
        input_tokens = _estimate_tokens(prompt)
        remaining = PLAN_CRITIC_TOKEN_BUDGET - spent_tokens - input_tokens
        if remaining < 256:
            raise PlanCriticError(
                f"Plan critic input exceeds the {PLAN_CRITIC_TOKEN_BUDGET}-token budget"
            )
        response = await llm.complete(
            critic_agent,
            [{"role": "user", "content": prompt}],
            max_tokens=min(_PLAN_CRITIC_MAX_OUTPUT_TOKENS, remaining),
            temperature=0.1,
            cwd=repo_root,
        )
        attempt_tokens = input_tokens + _estimate_tokens(response.text)
        spent_tokens += attempt_tokens
        if spent_tokens > PLAN_CRITIC_TOKEN_BUDGET:
            raise PlanCriticError(
                f"Plan critic exceeded the {PLAN_CRITIC_TOKEN_BUDGET}-token budget"
            )
        try:
            result = PlanCriticResult.model_validate(_parse_json(response.text))
            return result, spent_tokens
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            retry_reason = str(exc)

    raise PlanCriticError(
        f"Critic did not return a schema-valid verdict after {_MAX_ATTEMPTS} attempts: "
        f"{last_error}"
    )
