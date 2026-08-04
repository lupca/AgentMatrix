"""Real spec/plan generation: one LLM call, grounded in research-tool evidence.

Produces `SpecPlanResult` (positive outcomes, negative boundaries, reproducible
evidence, prior art, rejected alternatives, and enforced limits) for a
freshly created `Task`. `files` proposed by the LLM that the code graph
cannot confirm are annotated rather than trusted outright, and `flows` come
exclusively from `get_affected_flows` (the LLM never invents flow names).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError

logger = logging.getLogger(__name__)

from app.db.models import Agent, AgentRun, LLMUsage, Task
from app.schemas.task import (
    PLAN_CRITIC_RESULT_SCHEMA_VERSION,
    SPEC_PLAN_RESULT_SCHEMA_VERSION,
    PlanCriticResult,
    SpecPlanResult,
)
from app.services.graph_client import get_affected_flows, semantic_search
from app.services.cli_command import build_cli_command
from app.services.cli_dispatcher import route_model
from app.services.llm_client import calculate_cost
from app.services.llm_service import ConfigurationError, LLMService
from app.core.config import settings

UNCONFIRMED_SUFFIX = " *(chưa xác nhận)*"
_MAX_ATTEMPTS = 2
# Raised 50k -> 150k on 2026-08-04. VOMA-033's critic blew the 50k cap on a
# normal-sized plan, and the cap is meant to stop a runaway critic, not to make
# ordinary plans unreviewable.
#
# Why 150k is still cheap: one execute run measures ~1.03M tokens. The critic
# exists to prevent extra execute rounds (0.474 -> 0 extra rounds/task in the
# before/after sample), so each round it saves is worth ~7x this whole budget.
#
# Note the estimator below counts UTF-8 *bytes*/3. Vietnamese runs 2-3 bytes per
# character, so Vietnamese plans are charged well above their real token count —
# part of why 50k bound sooner than it looks.
PLAN_CRITIC_TOKEN_BUDGET = 150_000
_PLAN_CRITIC_MAX_OUTPUT_TOKENS = 4_096
_SEARCH_QUERY_MAX_CHARS = 500
_PRIOR_PLAN_MAX_CHARS = 25_000
_PRIOR_PLAN_TRUNCATION_NOTICE = "\n\n[... prior plan truncated to fit 25KB cap ...]"
_GRAPH_UNAVAILABLE_WARNING = (
    "The code graph search failed or returned no results. This plan was generated "
    "without repository grounding — treat file paths and flow references as unverified."
)


def _agent_cli(agent: Agent) -> str:
    cli = str(getattr(agent, "cli", "") or "").strip().lower()
    if cli:
        return cli
    return route_model(str(getattr(agent, "model", "") or "")).cli


def _begin_llm_run(
    db,
    task: Task,
    agent: Agent,
    prompt: str,
    *,
    kind: str,
) -> AgentRun | None:
    """Create a local, non-dispatch AgentRun for a planner/critic request.

    Planner calls are synchronous service work, not task dispatches, so they
    do not go through the dispatch outbox or a worker. Reusing the existing
    execute/review kinds keeps the no-migration schema contract intact while
    still making every request and its token estimate queryable.
    """
    if db is None:
        return None
    cli = _agent_cli(agent)
    model = str(getattr(agent, "model", "") or "")
    run = AgentRun(
        id=str(uuid.uuid4()),
        task_id=task.id,
        agent_id=str(agent.id),
        cli=cli,
        command=build_cli_command(
            cli,
            model,
            prompt,
            effort=getattr(agent, "effort", None),
            timeout_seconds=settings.RUN_TIMEOUT_SECONDS,
        ),
        kind=kind,
        agent_role="reviewer" if kind == "review" else "executor",
        status="running",
        timeout_seconds=settings.RUN_TIMEOUT_SECONDS,
        max_attempts=1,
        idempotency_key=f"planner:{task.id}:{kind}:{uuid.uuid4().hex[:16]}",
    )
    db.add(run)
    # COMMIT, not flush. The caller is about to await an LLM call that measures
    # 130-350s, and a flushed-but-uncommitted INSERT holds row locks for that
    # entire window.
    #
    # Observed twice on 2026-08-04 (pg_stat_activity during the outage):
    #   55772  idle in transaction  653s  INSERT INTO agent_runs ...   <- holds
    #   55591  blocked by {55772}   312s  SELECT tasks ...             <- waits
    # The waiter is a synchronous psycopg2 call running on the event loop, so
    # one blocked query stops the whole MCP server accepting connections: the
    # listening socket stays open, Recv-Q climbs (17, then 18), and every tool
    # call hangs with no error and no traceback. Recovery needed a restart.
    #
    # Committing here releases the locks in milliseconds. The row is meant to
    # outlive the call anyway — a run that later fails must stay queryable, and
    # _finish_llm_run commits its terminal state separately.
    db.commit()
    return run


def _finish_llm_run(
    db,
    run: AgentRun | None,
    response,
    *,
    started: float,
    operation: str,
    error: str | None = None,
) -> None:
    if db is None or run is None:
        return
    usage = response.usage if response is not None else None
    input_tokens = max(0, int(getattr(usage, "input_tokens", 0) or 0))
    output_tokens = max(0, int(getattr(usage, "output_tokens", 0) or 0))
    provider = str(getattr(response, "provider", "") or "cli")
    model = str(getattr(response, "model", "") or "")
    if response is not None:
        db.add(
            LLMUsage(
                task_id=run.task_id,
                agent_run_id=run.id,
                model=model,
                provider=provider,
                operation=operation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=0,
                cost_usd=calculate_cost(model, provider, input_tokens, output_tokens),
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        text = str(getattr(response, "text", "") or "")
        run.output_bytes = len(text.encode("utf-8"))
        run.output_lines = text.count("\n") + (1 if text else 0)
    run.status = "failed" if error else "success"
    run.error_message = error
    run.exit_code = 1 if error else 0
    run.completed_at = datetime.now(timezone.utc)
    # Commit for the same reason as _begin_llm_run: the planner path runs a
    # second LLM call (the critic) right after this one, so a transaction left
    # open here would hold locks across that call too.
    db.commit()


class SpecPlanGenerationError(RuntimeError):
    """The LLM did not produce a schema-valid spec/plan after retrying."""


class PlanCriticError(RuntimeError):
    """The independent critic could not produce a valid in-budget verdict."""


def _build_search_query(task: Task) -> str:
    """Combine raw_input and title into a single search query for the code graph.

    The title alone is often too short or abstract (e.g. "Fix auth bug") to
    surface relevant files.  ``raw_input`` carries the coordinator's investigation
    and is a much richer signal.  We concatenate both, truncate, and deduplicate
    so the graph gets the best single-query shot it can.
    """
    parts: list[str] = []
    raw = (task.raw_input or "").strip()
    title = (task.title or "").strip()
    if raw:
        parts.append(raw)
    if title and title.lower() not in (raw or "").lower():
        parts.append(title)
    query = " ".join(parts) if parts else title
    return query[:_SEARCH_QUERY_MAX_CHARS]


def _build_prompt(
    task: Task,
    graph_candidates: list[str],
    *,
    retry_reason: str | None = None,
    project_context: str | None = None,
    graph_warning: str | None = None,
) -> str:
    candidates = "\n".join(f"- {c}" for c in graph_candidates) or "(none found)"
    retry_note = (
        f"\nYour previous reply was rejected: {retry_reason}. "
        "Reply again with ONLY the corrected JSON object.\n"
        if retry_reason
        else ""
    )
    warning_block = (
        f"⚠ IMPORTANT: {graph_warning}\n"
        "Set spec_clarity to \"low\" and add a note in open_questions that the "
        "plan lacks repository grounding.\n\n"
        if graph_warning
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
    prior_plan_text = (getattr(task, "plan", None) or "").strip()
    if prior_plan_text:
        if len(prior_plan_text) > _PRIOR_PLAN_MAX_CHARS:
            keep = _PRIOR_PLAN_MAX_CHARS - len(_PRIOR_PLAN_TRUNCATION_NOTICE)
            prior_plan_text = prior_plan_text[: max(0, keep)] + _PRIOR_PLAN_TRUNCATION_NOTICE
        prior_plan_block = (
            "Prior-round plan and coordinator decisions (from task.plan — this is the "
            "result the coordinator saved from a previous generate_spec_plan run, "
            "including any answers to open questions the coordinator placed here via "
            "update_task). Use it as input: respect stated constraints and coordinator "
            "answers, do not copy the plan text blindly, and produce a fresh plan that "
            "reflects these decisions):\n"
            f"{prior_plan_text}\n\n"
        )
    else:
        prior_plan_block = ""
    project_id_json = json.dumps(task.project, ensure_ascii=False)
    return (
        "You are a software spec/plan generator for a task-coordination system.\n"
        f"Task title: {task.title}\n"
        f"Project: {task.project}\n\n"
        f"{details_block}"
        f"{context_block}"
        f"{prior_plan_block}"
        f"{warning_block}"
        "Bạn đang đứng TRONG repo của project. Hãy ĐỌC (read-only, không sửa gì) "
        "các file liên quan tới task — bắt đầu từ README/docs/entry points rồi "
        "lần theo — TRƯỚC KHI viết plan. Dựa trên những gì đã đọc: nếu spec còn "
        "mơ hồ (auth dùng gì, liên kết module nào, convention nào...) thì đặt câu "
        "hỏi cụ thể vào open_questions và chấm spec_clarity tương ứng.\n\n"
        "BẮT BUỘC tra kho living spec qua MCP trước khi kết luận prior_art hoặc "
        "constraints. Trên MCP native tool được expose trực tiếp: gọi "
        f'`spec_get({{"filter":{{"project_id":{project_id_json}}}}})` để lấy '
        "toàn bộ spec của project trong một lần. Không truy vấn DB trực tiếp và "
        "không bỏ qua bước này dù code hiện tại có vẻ đủ rõ. Đọc kết quả theo "
        "đúng thứ tự ưu tiên sau (thà lấy thừa spec còn hơn bỏ sót):\n"
        "  1. Negative boundaries: quan hệ conflicts_with và item "
        "kind=constraint — kiểm tra TRƯỚC; vi phạm là hỏng.\n"
        "  2. Existing system: item kind=requirement và kind=design trong cùng project.\n"
        "  3. Code location: anchors của các item liên quan để biết file/symbol nào "
        "hiện thực hoặc bị ràng buộc.\n"
        "  4. Delivery history: task_links của các item liên quan để biết task nào "
        "đã implements/modifies/references chúng.\n"
        "Nếu task chạm path/symbol có item constraint hoặc một đầu của "
        "conflicts_with, constraints PHẢI nêu ranh giới đó. Khi kho có prior art "
        "liên quan, prior_art PHẢI dẫn `spec_item:<id>` cụ thể (kèm title/kết luận), "
        "không được chỉ nói rằng đã đọc code. Ghi exact MCP query và phần kết quả "
        "được dùng vào evidence với source_type=query.\n\n"
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
    # Models that ignore "ONLY JSON" wrap the object in prose. Skip anything
    # before the first '{' ...
    if not text.startswith("{"):
        start = text.find("{")
        if start != -1:
            text = text[start:]
    # ... and let raw_decode stop at the end of the first complete object,
    # ignoring whatever follows.
    #
    # Trailing content used to be fatal: the old recovery was gated on
    # ``not text.startswith("{")``, so output that *began* with a valid object
    # and then added a closing sentence skipped recovery entirely and died in
    # json.loads with "Extra data: line 2 column 1". Observed live on VOMA-033,
    # where the agy plan critic failed that way three times in a row while the
    # claude planner — same parser, no trailing prose — succeeded.
    #
    # raw_decode also replaces the old rfind("}") span, which silently picked
    # the wrong closing brace whenever prose after the object contained one.
    decoded, _ = json.JSONDecoder().raw_decode(text)
    if not isinstance(decoded, dict):
        raise json.JSONDecodeError(
            f"expected a JSON object, got {type(decoded).__name__}", text, 0
        )
    return decoded


async def generate_spec_plan(
    task: Task,
    repo_root: str | None,
    agent: Agent,
    project_context: str | None = None,
    db=None,
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
    graph_warning: str | None = None
    if repo_root:
        search_query = _build_search_query(task)
        try:
            found = await semantic_search(
                repo_root, search_query, limit=15, raise_on_error=True
            )
            if isinstance(found, list):
                graph_candidates = [
                    str(item.get("file_path") or item.get("name") or item)
                    for item in found
                    if isinstance(item, dict)
                ]
            if not graph_candidates:
                logger.warning(
                    "semantic_search returned no results for task %s (query=%r)",
                    task.id,
                    search_query[:120],
                )
                graph_warning = _GRAPH_UNAVAILABLE_WARNING
        except Exception as exc:
            logger.warning(
                "semantic_search failed for task %s: %s", task.id, exc,
            )
            graph_candidates = []
            graph_warning = (
                f"{_GRAPH_UNAVAILABLE_WARNING} (search error: {exc})"
            )

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
            graph_warning=graph_warning,
        )
        run = _begin_llm_run(db, task, agent, prompt, kind="execute")
        started = time.monotonic()
        response = None
        try:
            response = await llm.complete(
                agent,
                [{"role": "user", "content": prompt}],
                # This remains an API-provider output hint only. CLIProvider
                # deliberately does not truncate the CLI stream.
                max_tokens=4096,
                temperature=0.3,
                cwd=repo_root,
            )
        except Exception as exc:
            _finish_llm_run(
                db, run, None, started=started, operation="plan", error=str(exc)
            )
            raise
        content = response.text
        try:
            parsed = _parse_json(content)
            result = SpecPlanResult.model_validate(parsed)
            _finish_llm_run(db, run, response, started=started, operation="plan")
            last_error = None
            break
        except (json.JSONDecodeError, ValidationError) as exc:
            _finish_llm_run(
                db, run, response, started=started, operation="plan", error=str(exc)
            )
            last_error = exc
            retry_reason = str(exc)
            continue

    if result is None:
        raise SpecPlanGenerationError(
            f"LLM did not return a schema-valid spec/plan after "
            f"{_MAX_ATTEMPTS} attempts: {last_error}"
        )

    if graph_warning:
        clarity_order = {"high": 2, "medium": 1, "low": 0}
        current = clarity_order.get(result.spec_clarity, 0)
        updates: dict = {}
        if current > 0:
            updates["spec_clarity"] = "low"
        grounding_note = (
            "Code graph search failed or returned no results — this plan was "
            "generated without repository grounding. File paths and flow "
            "references are unverified."
        )
        existing_questions = list(result.open_questions)
        if grounding_note not in existing_questions:
            updates["open_questions"] = [grounding_note] + existing_questions
        if updates:
            result = result.model_copy(update=updates)

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
    project_id_json = json.dumps(task.project, ensure_ascii=False)
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
        "you MUST first call the MCP tool "
        f'`spec_get({{"filter":{{"project_id":{project_id_json}}}}})` before '
        "evaluating prior_art. Check spec_item, relations, anchors, and "
        "spec_task_link; a prior_art claim backed by the living spec must cite a "
        "concrete spec_item id. You may use targeted git history only after that "
        "mandatory spec lookup to check claimed prior art.\n\n"
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
    db=None,
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
        run = _begin_llm_run(db, task, critic_agent, prompt, kind="review")
        started = time.monotonic()
        response = None
        try:
            response = await llm.complete(
                critic_agent,
                [{"role": "user", "content": prompt}],
                max_tokens=min(_PLAN_CRITIC_MAX_OUTPUT_TOKENS, remaining),
                temperature=0.1,
                cwd=repo_root,
            )
        except Exception as exc:
            _finish_llm_run(
                db, run, None, started=started, operation="plan_critic", error=str(exc)
            )
            raise
        attempt_tokens = input_tokens + _estimate_tokens(response.text)
        spent_tokens += attempt_tokens
        if spent_tokens > PLAN_CRITIC_TOKEN_BUDGET:
            _finish_llm_run(
                db,
                run,
                response,
                started=started,
                operation="plan_critic",
                error=(
                    f"Plan critic exceeded the {PLAN_CRITIC_TOKEN_BUDGET}-token budget"
                ),
            )
            raise PlanCriticError(
                f"Plan critic exceeded the {PLAN_CRITIC_TOKEN_BUDGET}-token budget"
            )
        try:
            result = PlanCriticResult.model_validate(_parse_json(response.text))
            _finish_llm_run(
                db, run, response, started=started, operation="plan_critic"
            )
            return result, spent_tokens
        except (json.JSONDecodeError, ValidationError) as exc:
            _finish_llm_run(
                db,
                run,
                response,
                started=started,
                operation="plan_critic",
                error=str(exc),
            )
            last_error = exc
            retry_reason = str(exc)

    raise PlanCriticError(
        f"Critic did not return a schema-valid verdict after {_MAX_ATTEMPTS} attempts: "
        f"{last_error}"
    )
