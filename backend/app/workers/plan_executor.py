"""Worker-side execution for planner/plan-critic AgentRuns (CTV2-1382).

Before this module existed, ``generate_spec_plan``/``criticize_spec_plan``
were awaited directly inside the MCP request handler -- synchronous,
in-process LLM calls that blocked the MCP server for 130-420s and left their
AgentRun rows with ``pid IS NULL`` and ``started_at IS NULL`` (never claimed
by a worker). This module runs those same two functions (unchanged) from
inside the Dramatiq worker instead, reached through the identical
transactional-outbox + ``run_agent`` path every other AgentRun uses --
``cli_executor.execute_agent_run`` recognizes a planner/critic run by its
``idempotency_key`` prefix (see ``is_plan_run``) and delegates here instead
of running the git-worktree/diff dispatch path, which does not apply to a
read-only research call.

A planner/critic AgentRun never drives ``Task.status`` (the task stays
``todo`` throughout planning), so failures here are recorded locally on the
AgentRun row rather than through ``TaskOrchestrationService.record_*_failure``
-- those all assert a dispatch-flow status (``dispatched``/``in-review``) that
a planning task will never be in.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Agent, AgentRun, Project, Task, TaskEvent
from app.services import spec_plan_generator
from app.services.agent_suggester import AgentSuggester
from app.services.context_generator import get_matching_rules
from app.services.llm_service import ConfigurationError
from app.services.outbox import record_run_requested
from app.services.spec_plan_generator import PlanCriticError, SpecPlanGenerationError
from app.services.task_event_service import emit_task_event
from app.services.task_orchestration import TaskOrchestrationService
from app.services.tool_metrics import record_tool_metric

logger = logging.getLogger(__name__)

PLANNER_PREFIX = "planner:"
DISPATCH_CONTEXT_EVENT = "spec_plan_dispatch_context"


def is_plan_run(run: AgentRun) -> bool:
    """True for an AgentRun created by generate_spec_plan/critique_spec_plan."""
    return (run.idempotency_key or "").startswith(PLANNER_PREFIX)


def _step_kind(run: AgentRun) -> str:
    # idempotency_key shape: "planner:{task_id}:{step}:{suffix}", step in
    # {"plan", "critic"} -- task_id itself may contain ':' only if a caller
    # constructed it oddly, which is why this splits from a fixed prefix
    # instead of by position from the right.
    rest = (run.idempotency_key or "")[len(PLANNER_PREFIX):]
    parts = rest.split(":")
    return parts[-2] if len(parts) >= 3 else ""


def build_project_context(
    db: Session, project: Project | None, files: list[str] | None
) -> str | None:
    context_parts: list[str] = []
    if project is not None and (project.context_md or "").strip():
        context_parts.append(project.context_md.strip())
    if project is not None:
        for rule in get_matching_rules(db, project.id, files or None):
            context_parts.append(f"## Rule: {rule.name}\n{rule.content}")
    return "\n\n".join(context_parts) or None


def dispatch_plan_run(db: Session, run: AgentRun, repo_root: str) -> None:
    """Write the outbox row for `run` and best-effort fast-path enqueue it.

    Mirrors ``agent_runner._enqueue_run``'s fast path, but deliberately never
    calls ``record_dispatch_queue_failure`` on a send failure -- that helper
    asserts ``task.status == 'dispatched'``, which a planner/critic task
    (status stays ``todo`` throughout) never satisfies. A failed fast path
    just leaves the outbox event pending for ``publish_pending_events`` (the
    outbox_publisher worker) to retry.
    """
    record_run_requested(db, run, repo_root)
    db.commit()
    try:
        from app.workers.agent_runner import run_agent

        message = run_agent.send(
            run.id, run.task_id, run.command, repo_root, run.timeout_seconds
        )
        message_id = getattr(message, "message_id", None)
        if message_id:
            run.dramatiq_message_id = str(message_id)
            db.commit()
    except Exception:
        logger.warning(
            "planner: fast-path enqueue failed for run %s; outbox publisher will retry",
            run.id,
            exc_info=True,
        )


def _fail_run(db: Session, run: AgentRun, error: object) -> None:
    run.status = "failed"
    run.pid = None
    run.exit_code = 1
    run.error_message = str(error)[:4000]
    run.completed_at = datetime.now(timezone.utc)
    db.commit()


def execute_plan_run(db: Session, run: AgentRun, task: Task, timeout_seconds: int) -> None:
    """Entry point called from ``cli_executor.execute_agent_run`` for a
    claimed (status=='running') planner/critic AgentRun."""

    step = _step_kind(run)
    project = db.get(Project, task.project) if task.project else None
    repo_root = os.path.abspath(project.repo_root) if project and project.repo_root else None
    try:
        if step == "plan":
            _run_plan_step(db, run, task, project, repo_root)
        elif step == "critic":
            _run_critic_step(db, run, task, project, repo_root)
        else:
            raise ValueError(
                f"Unrecognized planner step in idempotency_key {run.idempotency_key!r}"
            )
    except (ConfigurationError, SpecPlanGenerationError, PlanCriticError) as exc:
        logger.warning("planner: step %s failed for run %s: %s", step, run.id, exc)
        _fail_run(db, run, exc)
    except Exception as exc:  # noqa: BLE001 - never let a planner crash strand the run
        logger.exception("planner: step %s crashed for run %s", step, run.id)
        _fail_run(db, run, exc)


def _run_plan_step(
    db: Session, run: AgentRun, task: Task, project: Project | None, repo_root: str | None
) -> None:
    agent = db.get(Agent, run.agent_id)
    if agent is None:
        raise ConfigurationError(f"Planner agent {run.agent_id} no longer exists")
    project_context = build_project_context(db, project, task.files)

    # Close the read transaction the lines above opened before entering a call
    # that shells out to code-review-graph and then to an LLM.
    #
    # `_begin_llm_run` commits (the 6cf5edf fix), but it runs *after*
    # `await semantic_search(...)` inside generate_spec_plan -- so the reads
    # here stayed open across the graph subprocess.  That is the same shape
    # that held a transaction for 653s and hung the MCP server twice on
    # 2026-08-04; it only stopped being an outage because CTV2-1382 moved this
    # work into the worker.  Commit before any long call, not just before the
    # LLM one.
    db.commit()

    result, flows = asyncio.run(
        spec_plan_generator.generate_spec_plan(
            task, repo_root, agent, project_context=project_context, db=db, run=run,
        )
    )
    record_tool_metric(
        tool="spec_plan",
        source="spec_plan_generator",
        ok=True,
        task_id=task.id,
        result_count=len(result.open_questions),
        payload={"spec_clarity": result.spec_clarity, "task_id": task.id},
    )
    service = TaskOrchestrationService(db)
    service.write_spec_plan(
        task_id=task.id,
        actor=f"worker:plan:{run.id}",
        acceptance_criteria=result.acceptance_criteria,
        constraints=result.constraints,
        evidence=[item.model_dump(mode="json") for item in result.evidence],
        prior_art=result.prior_art,
        ruled_out=[item.model_dump(mode="json") for item in result.ruled_out],
        limits=result.limits.model_dump(mode="json") if result.limits else None,
        plan=result.plan,
        files=result.files,
        tests=result.tests,
        risk=result.risk,
        flows=flows,
        spec_clarity=result.spec_clarity,
        open_questions=result.open_questions,
        planner=agent.id,
    )
    db.refresh(task)
    _dispatch_critic_step(db, task, repo_root, planner_agent=agent)


def _resolve_critic_agent(
    db: Session, task: Task, *, planner_agent_id: str, explicit_critic_id: str
) -> Agent | None:
    if explicit_critic_id:
        return db.get(Agent, explicit_critic_id)
    suggestions = AgentSuggester(db).suggest(
        task, role="reviewer", top_n=10, exclude_agent_id=planner_agent_id
    )
    for suggestion in suggestions:
        candidate = db.get(Agent, suggestion.agent_id)
        candidate_type = getattr(
            getattr(candidate, "agent_type", None), "value", None
        ) or getattr(candidate, "agent_type", "")
        if candidate is not None and str(candidate_type).strip().lower() != "api":
            return candidate
    return None


def _record_dispatch_failure(db: Session, task: Task, error: str) -> None:
    """Surface a critic-dispatch failure without touching Task.status.

    Recorded as a decision event so ``get_status``/``wait_for_task`` surface
    it the same way a dispatch escalation would; the plan itself stays valid
    on the task (only the critic step could not be scheduled), so a human can
    retry via ``critique_spec_plan``.
    """
    emit_task_event(
        task_id=task.id,
        event_type="run_failed",
        kind="decision",
        payload={"step": "plan_critic_dispatch", "error": error},
        db=db,
    )


def create_plan_run(db: Session, task: Task, *, agent: Agent, step: str) -> AgentRun:
    """Insert (but do not dispatch) the AgentRun envelope for one planner step.

    ``command``/``cli`` are placeholders: the real prompt depends on a
    semantic-search call (plan step) or the just-written plan (critic step),
    both of which only the worker call to ``generate_spec_plan`` /
    ``criticize_spec_plan`` can produce -- and both overwrite this row's
    ``command``/``cli`` via ``_begin_llm_run(..., run=run)`` before the CLI is
    ever spawned. The outbox/dramatiq ``command`` argument is therefore
    unused for a planner run; ``cli_executor.execute_agent_run`` routes it to
    ``execute_plan_run``, which never reads the parameter.
    """
    kind = "execute" if step == "plan" else "review"
    run = AgentRun(
        id=str(uuid.uuid4()),
        task_id=task.id,
        agent_id=str(agent.id),
        cli=str(getattr(agent, "cli", "") or "claude"),
        command=f"# spec-plan {step} pending for task {task.id}",
        kind=kind,
        agent_role="reviewer" if kind == "review" else "executor",
        status="queued",
        timeout_seconds=settings.RUN_TIMEOUT_SECONDS,
        max_attempts=1,
        idempotency_key=f"planner:{task.id}:{step}:{uuid.uuid4().hex[:16]}",
    )
    db.add(run)
    return run


def create_critic_run(
    db: Session, task: Task, repo_root: str | None, *, critic_agent: Agent,
) -> AgentRun:
    """Insert and dispatch the critic AgentRun for `task`'s current plan.

    Four-eyes (critic != task.planner) is validated by the caller before this
    is reached, and re-validated from DB when the run actually executes (see
    ``criticize_spec_plan`` -> ``require_independent``) -- not repeated here.
    """
    critic_run = create_plan_run(db, task, agent=critic_agent, step="critic")
    dispatch_plan_run(db, critic_run, repo_root or "")
    return critic_run


def _dispatch_critic_step(
    db: Session, task: Task, repo_root: str | None, *, planner_agent: Agent,
) -> None:
    pending = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.event_type == DISPATCH_CONTEXT_EVENT)
        .order_by(TaskEvent.id.desc())
        .first()
    )
    explicit_critic_id = ""
    if pending is not None and isinstance(pending.payload, dict):
        explicit_critic_id = str(pending.payload.get("critic_id") or "").strip()

    critic_agent = _resolve_critic_agent(
        db, task, planner_agent_id=planner_agent.id, explicit_critic_id=explicit_critic_id,
    )
    if critic_agent is None:
        reason = (
            f"Critic agent {explicit_critic_id} not found"
            if explicit_critic_id
            else "No independent CLI plan critic is available"
        )
        _record_dispatch_failure(db, task, reason)
        return

    create_critic_run(db, task, repo_root, critic_agent=critic_agent)


def _run_critic_step(
    db: Session, run: AgentRun, task: Task, project: Project | None, repo_root: str | None
) -> None:
    if not task.planner:
        raise PlanCriticError(f"Task {task.id} has no plan to critique yet")
    planner_agent = db.get(Agent, task.planner)
    if planner_agent is None:
        raise ConfigurationError(f"Planner agent {task.planner} no longer exists")
    critic_agent = db.get(Agent, run.agent_id)
    if critic_agent is None:
        raise ConfigurationError(f"Critic agent {run.agent_id} no longer exists")

    plan_from_db = spec_plan_generator.spec_plan_result_from_task(task)
    project_context = build_project_context(db, project, task.files)

    # Same reason as _run_plan_step: release the read transaction before the
    # long call, not somewhere inside it.
    db.commit()

    critic_result, critic_tokens = asyncio.run(
        spec_plan_generator.criticize_spec_plan(
            task,
            plan_from_db,
            repo_root,
            planner_agent,
            critic_agent,
            project_context=project_context,
            db=db,
            run=run,
        )
    )
    record_tool_metric(
        tool="plan_critic",
        source="spec_plan_generator",
        ok=True,
        task_id=task.id,
        result_count=len(critic_result.findings),
        payload={
            "verdict": critic_result.verdict,
            "critic": critic_agent.id,
            "planner": task.planner,
            "tokens_used": critic_tokens,
            "token_budget": spec_plan_generator.PLAN_CRITIC_TOKEN_BUDGET,
            "diff_provided": False,
        },
    )
    service = TaskOrchestrationService(db)
    service.record_plan_critic_verdict(
        task_id=task.id,
        actor=f"worker:critic:{run.id}",
        critic=critic_agent.id,
        verdict=critic_result.verdict,
        findings=[item.model_dump(mode="json") for item in critic_result.findings],
        summary=critic_result.summary,
        tokens=critic_tokens,
    )


__all__ = [
    "PLANNER_PREFIX",
    "DISPATCH_CONTEXT_EVENT",
    "is_plan_run",
    "build_project_context",
    "dispatch_plan_run",
    "create_plan_run",
    "create_critic_run",
    "execute_plan_run",
]
