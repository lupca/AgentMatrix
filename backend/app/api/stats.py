from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import (
    Agent as AgentModel,
    LLMUsage,
    ModelPricing,
    Project as ProjectModel,
    Task as TaskModel,
)
from app.schemas.stats import ProjectStats, AgentStats

router = APIRouter(prefix="/api/stats", tags=["stats"])

V1_INPUT_TOKENS_PER_CYCLE = 3_575


def _usage_query(
    db: Session,
    session_id: str | None = None,
    task_id: str | None = None,
    operation: str | None = None,
):
    query = db.query(LLMUsage)
    if session_id:
        query = query.filter(LLMUsage.session_id == session_id)
    if task_id:
        query = query.filter(LLMUsage.task_id == task_id)
    if operation:
        query = query.filter(LLMUsage.operation == operation)
    return query


def _usage_totals(rows: list[LLMUsage]) -> dict[str, Any]:
    input_tokens = sum(row.input_tokens or 0 for row in rows)
    output_tokens = sum(row.output_tokens or 0 for row in rows)
    cached_tokens = sum(row.cached_tokens or 0 for row in rows)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    cost = sum((row.cost_usd or Decimal("0") for row in rows), Decimal("0"))
    latency = sum(row.latency_ms or 0 for row in rows)
    return {
        "calls": len(rows),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "uncached_tokens": uncached_tokens,
        # Cached input is a subset of input, so it must not be double counted.
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(float(cost), 8),
        "average_latency_ms": round(latency / len(rows), 2) if rows else 0,
    }


def _turn_breakdown(
    rows: list[LLMUsage], index_rows: list[LLMUsage] | None = None
) -> list[dict]:
    """Cached vs uncached tokens for each individual call (turn), not summed.

    The aggregate totals in ``_usage_totals`` hide whether prompt-caching
    savings are per-turn or a fluke of one huge call; this exposes each row
    in insertion order (per session) so before/after pruning changes can be
    compared turn by turn.

    ``turn_index`` numbers a row by its position among ``index_rows``
    (defaults to ``rows``), which must be unfiltered by ``operation`` so an
    operation filter doesn't skip positions and misnumber the turn a row
    belongs to. Rows with no ``session_id`` are never grouped into one
    pseudo-session — each gets its own singleton index.
    """
    index_source = index_rows if index_rows is not None else rows

    def group_key(row: LLMUsage) -> str:
        return row.session_id if row.session_id else f"__no_session_{row.id}"

    ordered_index = sorted(index_source, key=lambda row: (row.session_id or "", row.id))
    counters: dict[str, int] = {}
    turn_index_by_id: dict[int, int] = {}
    for row in ordered_index:
        key = group_key(row)
        counters[key] = counters.get(key, 0) + 1
        turn_index_by_id[row.id] = counters[key]

    ordered = sorted(rows, key=lambda row: (row.session_id or "", row.id))
    breakdown = []
    for row in ordered:
        input_tokens = row.input_tokens or 0
        cached_tokens = row.cached_tokens or 0
        breakdown.append(
            {
                "turn_index": turn_index_by_id.get(row.id, 1),
                "session_id": row.session_id,
                "task_id": row.task_id,
                "operation": row.operation,
                "input_tokens": input_tokens,
                "output_tokens": row.output_tokens or 0,
                "cached_tokens": cached_tokens,
                "uncached_tokens": max(0, input_tokens - cached_tokens),
            }
        )
    return breakdown


def _usage_breakdown(rows: list[LLMUsage], attribute: str, key_name: str) -> list[dict]:
    grouped: dict[str | None, list[LLMUsage]] = {}
    for row in rows:
        key = getattr(row, attribute)
        grouped.setdefault(key, []).append(row)

    result = []
    for key, group_rows in grouped.items():
        result.append({key_name: key, **_usage_totals(group_rows)})
    return sorted(result, key=lambda item: item["total_tokens"], reverse=True)


@router.get("/overview")
def get_stats_overview(db: Session = Depends(get_db)):
    tasks = db.query(TaskModel).all()
    total_tasks = len(tasks)

    by_status: dict[str, int] = {}
    for task in tasks:
        st = task.status or "unknown"
        by_status[st] = by_status.get(st, 0) + 1

    done_tasks = by_status.get("done", 0) + by_status.get("completed", 0) + by_status.get("passed", 0)
    inactive_count = done_tasks + by_status.get("cancelled", 0) + by_status.get("failed", 0)
    active_tasks = max(0, total_tasks - inactive_count)

    return {
        "totalTasks": total_tasks,
        "completedTasks": done_tasks,
        "activeGates": active_tasks,
        "tasksByStatus": by_status,
    }


@router.get("/projects", response_model=list[ProjectStats])
def get_stats_projects(db: Session = Depends(get_db)):
    projects = db.query(ProjectModel).all()
    project_map = {p.id: p.name for p in projects}

    tasks = db.query(TaskModel).all()

    stats_by_project: dict[str, dict] = {}

    for proj in projects:
        stats_by_project[proj.id] = {
            "project_id": proj.id,
            "project_name": proj.name,
            "total_tasks": 0,
            "done_tasks": 0,
            "active_tasks": 0,
            "by_status": {}
        }

    for task in tasks:
        pid = task.project or "unassigned"
        if pid not in stats_by_project:
            stats_by_project[pid] = {
                "project_id": pid,
                "project_name": project_map.get(pid, pid),
                "total_tasks": 0,
                "done_tasks": 0,
                "active_tasks": 0,
                "by_status": {}
            }

        entry = stats_by_project[pid]
        entry["total_tasks"] += 1
        st = task.status or "unknown"
        entry["by_status"][st] = entry["by_status"].get(st, 0) + 1

    result = []
    for pid, data in stats_by_project.items():
        by_st = data["by_status"]
        done_cnt = by_st.get("done", 0) + by_st.get("completed", 0) + by_st.get("passed", 0)
        inactive_cnt = done_cnt + by_st.get("cancelled", 0) + by_st.get("failed", 0)
        act_cnt = max(0, data["total_tasks"] - inactive_cnt)

        result.append(
            ProjectStats(
                project_id=data["project_id"],
                project_name=data["project_name"],
                total_tasks=data["total_tasks"],
                done_tasks=done_cnt,
                active_tasks=act_cnt,
                by_status=by_st
            )
        )

    return result


@router.get("/tokens")
def get_token_stats(
    session_id: str | None = None,
    task_id: str | None = None,
    operation: str | None = None,
    db: Session = Depends(get_db),
):
    """Aggregate measured LLM usage, optionally scoped to one ledger dimension."""

    rows = _usage_query(db, session_id, task_id, operation).all()
    totals = _usage_totals(rows)

    # by_turn is per-row (unbounded by the grouping every other breakdown
    # applies), so it is only computed when scoped to a single session/task —
    # otherwise an unfiltered dashboard load would serialize the entire
    # ledger, one entry per LLM call ever made.
    by_turn: list[dict] = []
    if session_id or task_id:
        index_rows = (
            _usage_query(db, session_id, task_id, operation=None).all()
            if operation
            else rows
        )
        by_turn = _turn_breakdown(rows, index_rows)

    return {
        "total_calls": totals["calls"],
        "total_input_tokens": totals["input_tokens"],
        "total_output_tokens": totals["output_tokens"],
        "total_cached_tokens": totals["cached_tokens"],
        "total_uncached_tokens": totals["uncached_tokens"],
        "total_tokens": totals["total_tokens"],
        "total_cost_usd": totals["cost_usd"],
        "average_latency_ms": totals["average_latency_ms"],
        "totals": totals,
        "by_session": _usage_breakdown(rows, "session_id", "session_id"),
        "by_task": _usage_breakdown(rows, "task_id", "task_id"),
        "by_operation": _usage_breakdown(rows, "operation", "operation"),
        "by_model": _usage_breakdown(rows, "model", "model"),
        "by_provider": _usage_breakdown(rows, "provider", "provider"),
        "by_turn": by_turn,
    }


@router.get("/tokens/comparison")
def get_token_comparison(
    session_id: str | None = None,
    task_id: str | None = None,
    operation: str | None = None,
    db: Session = Depends(get_db),
):
    """Compare V2 measured input usage with the historical V1 cycle baseline."""

    rows = _usage_query(db, session_id, task_id, operation).all()
    session_cycles = {row.session_id for row in rows if row.session_id}
    task_cycles = {row.task_id for row in rows if row.task_id}
    cycle_count = len(session_cycles) or len(task_cycles) or (1 if rows else 0)
    v2_input_tokens = sum(row.input_tokens or 0 for row in rows)
    v2_input_tokens_per_cycle = (
        v2_input_tokens / cycle_count if cycle_count else 0
    )
    v1_estimated_input_tokens = V1_INPUT_TOKENS_PER_CYCLE * cycle_count
    reduction = (
        (1 - v2_input_tokens_per_cycle / V1_INPUT_TOKENS_PER_CYCLE) * 100
        if cycle_count
        else 0
    )
    tokens_saved = v1_estimated_input_tokens - v2_input_tokens

    return {
        "baseline_input_tokens_per_cycle": V1_INPUT_TOKENS_PER_CYCLE,
        "v1_baseline_tokens_per_cycle": V1_INPUT_TOKENS_PER_CYCLE,
        "cycle_count": cycle_count,
        "v1_estimated_input_tokens": v1_estimated_input_tokens,
        "v2_input_tokens": v2_input_tokens,
        "v2_input_tokens_per_cycle": round(v2_input_tokens_per_cycle, 2),
        "tokens_saved": tokens_saved,
        "reduction_percentage": round(reduction, 2),
        "reduction_percent": round(reduction, 2),
        "target_reduction_percentage": 80,
        "target_met": reduction >= 80 if cycle_count else False,
    }


@router.get("/agents", response_model=list[AgentStats])
def get_stats_agents(db: Session = Depends(get_db)):
    agents = db.query(AgentModel).all()
    tasks = db.query(TaskModel).all()

    agent_stats: dict[str, dict] = {}

    for agent in agents:
        agent_stats[agent.id] = {
            "agent_id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "tasks_executed": 0,
            "tasks_reviewed": 0,
            "tasks_completed": 0,
            "active_tasks": 0,
        }

    for task in tasks:
        st = task.status or "unknown"
        is_done = st in ("done", "completed", "passed")
        is_active = st not in ("done", "completed", "passed", "cancelled", "failed")

        if task.executor:
            if task.executor not in agent_stats:
                agent_stats[task.executor] = {
                    "agent_id": task.executor,
                    "name": task.executor,
                    "role": "executor",
                    "tasks_executed": 0,
                    "tasks_reviewed": 0,
                    "tasks_completed": 0,
                    "active_tasks": 0,
                }
            agent_stats[task.executor]["tasks_executed"] += 1
            if is_done:
                agent_stats[task.executor]["tasks_completed"] += 1
            if is_active:
                agent_stats[task.executor]["active_tasks"] += 1

        if task.reviewer:
            if task.reviewer not in agent_stats:
                agent_stats[task.reviewer] = {
                    "agent_id": task.reviewer,
                    "name": task.reviewer,
                    "role": "reviewer",
                    "tasks_executed": 0,
                    "tasks_reviewed": 0,
                    "tasks_completed": 0,
                    "active_tasks": 0,
                }
            agent_stats[task.reviewer]["tasks_reviewed"] += 1

    result = []
    for aid, data in agent_stats.items():
        executed = data["tasks_executed"]
        completed = data["tasks_completed"]
        success_rate = (completed / executed) if executed > 0 else 1.0

        result.append(
            AgentStats(
                agent_id=data["agent_id"],
                name=data["name"],
                role=data["role"],
                tasks_executed=executed,
                tasks_reviewed=data["tasks_reviewed"],
                tasks_completed=completed,
                success_rate=round(success_rate, 4),
                active_tasks=data["active_tasks"],
            )
        )

    return result


@router.get("/pricing")
def get_model_pricing(
    provider: str | None = None,
    db: Session = Depends(get_db),
):
    """Get all model pricing information."""
    query = db.query(ModelPricing)
    if provider:
        query = query.filter(ModelPricing.provider == provider)

    rows = query.order_by(ModelPricing.provider, ModelPricing.model).all()

    return [
        {
            "id": row.id,
            "model": row.model,
            "provider": row.provider,
            "input_price_per_mtok": float(row.input_price_per_mtok) if row.input_price_per_mtok else None,
            "output_price_per_mtok": float(row.output_price_per_mtok) if row.output_price_per_mtok else None,
            "cached_input_price_per_mtok": float(row.cached_input_price_per_mtok) if row.cached_input_price_per_mtok else None,
            "cache_write_5m_per_mtok": float(row.cache_write_5m_per_mtok) if row.cache_write_5m_per_mtok else None,
            "cache_write_1h_per_mtok": float(row.cache_write_1h_per_mtok) if row.cache_write_1h_per_mtok else None,
            "notes": row.notes,
            "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        }
        for row in rows
    ]


@router.put("/pricing/{pricing_id}")
def update_model_pricing(
    pricing_id: int,
    input_price_per_mtok: float | None = None,
    output_price_per_mtok: float | None = None,
    cached_input_price_per_mtok: float | None = None,
    db: Session = Depends(get_db),
):
    """Update a model pricing entry."""
    row = db.query(ModelPricing).filter(ModelPricing.id == pricing_id).first()
    if not row:
        return {"error": "Pricing entry not found"}

    if input_price_per_mtok is not None:
        row.input_price_per_mtok = input_price_per_mtok
    if output_price_per_mtok is not None:
        row.output_price_per_mtok = output_price_per_mtok
    if cached_input_price_per_mtok is not None:
        row.cached_input_price_per_mtok = cached_input_price_per_mtok

    db.commit()
    return {"success": True, "id": pricing_id}
