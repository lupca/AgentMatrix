"""Queryable effectiveness report for the plan-critic intervention."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import GateRecord, TaskRound


def _round_cohort(db: Session, task_ids: set[str] | None) -> dict[str, Any]:
    query = db.query(TaskRound.task_id, func.count(TaskRound.id)).group_by(TaskRound.task_id)
    if task_ids is not None:
        if not task_ids:
            return {"tasks": 0, "extra_rounds": 0, "extra_rounds_per_task": None}
        query = query.filter(TaskRound.task_id.in_(task_ids))
    rows = query.all()
    extras = sum(max(0, int(count) - 1) for _, count in rows)
    return {
        "tasks": len(rows),
        "extra_rounds": extras,
        "extra_rounds_per_task": round(extras / len(rows), 3) if rows else None,
    }


def plan_critic_report(db: Session, *, task_id: str | None = None) -> dict[str, Any]:
    critic_query = db.query(GateRecord).filter(GateRecord.gate_type == "plan_critic")
    if task_id:
        critic_query = critic_query.filter(GateRecord.task_id == task_id)
    records = critic_query.all()
    returned = sum(1 for row in records if row.status == "rejected")
    critic_task_ids = {row.task_id for row in records}

    all_round_task_ids = {
        row[0]
        for row in db.query(TaskRound.task_id).distinct().all()
        if row[0] is not None
    }
    if task_id:
        all_round_task_ids &= {task_id}
    before_ids = all_round_task_ids - critic_task_ids
    after_ids = all_round_task_ids & critic_task_ids
    return {
        "plans_criticized": len(records),
        "plans_returned": returned,
        "return_rate": round(returned / len(records), 4) if records else None,
        "round_definition": "extra_rounds=max(execution_round_count-1, 0)",
        "before_critic": _round_cohort(db, before_ids),
        "after_critic": _round_cohort(db, after_ids),
    }
