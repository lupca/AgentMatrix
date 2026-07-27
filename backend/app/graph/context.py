"""Compact, structured context used by the user-chat coordinator.

The snapshot is deliberately built from relational data instead of chat
history.  That keeps read questions cheap while mutations continue to be
handled by the coordinator's tools/commands.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession, object_session

from app.db.models import (
    Agent,
    AgentType,
    Project,
    Session as SessionModel,
    SessionStatus,
    Task,
)


_CACHE_INFO_KEY = "control_tower_context_snapshot_cache"

# Enumeration cap for the "## System State" block: the rest of a large list
# is only counted, never listed, to keep the snapshot within the ~30 line /
# ~600 token hard cap regardless of how many projects/agents exist.
_TOP_N_PROJECT_NAMES = 8


def _database_for(
    session: SessionModel,
    db: DBSession | None,
) -> DBSession:
    if db is not None:
        return db
    attached = object_session(session)
    if attached is None:
        raise ValueError(
            "build_context_snapshot requires db for a detached Session model"
        )
    return attached


def _scope_project_id(session: SessionModel, db: DBSession) -> str | None:
    """Resolve the project scope, including task-scoped sessions."""

    if session.project_id:
        return session.project_id
    if not session.task_id:
        return None
    task = db.query(Task.project).filter(Task.id == session.task_id).first()
    return task[0] if task else None


def _clean_title(title: str | None, limit: int = 40) -> str:
    value = " ".join(str(title or "").split())
    if len(value) <= limit:
        return value
    return value[:limit]


def build_context_snapshot(
    session: SessionModel,
    db: DBSession | None = None,
) -> str:
    """Generate a compact "## System State" summary of the whole database.

    Every count below is one aggregate query; only projects get a bounded
    name enumeration (top ``_TOP_N_PROJECT_NAMES``, remainder just counted)
    so the block stays within the ~30 line / ~600 token hard cap regardless
    of how many projects/agents/sessions/tasks exist. Long-tail reads go
    through the ``query_db`` tool instead of growing this snapshot.

    The five most recently updated tasks are appended as well: scoped to
    the session's project when it has one, or across all projects for a
    global session (so the busiest chat context isn't left with an empty
    task list).  ``db`` is optional for attached ORM instances, which keeps
    this function convenient in unit tests while allowing callers to pass
    an explicit database session.
    """

    db = _database_for(session, db)

    projects = (
        db.query(Project)
        .filter(Project.status == "active")
        .order_by(Project.id.asc())
        .all()
    )
    project_names = [
        _clean_title(project.name, 24) for project in projects[:_TOP_N_PROJECT_NAMES]
    ]
    if len(projects) > _TOP_N_PROJECT_NAMES:
        project_names.append(f"+{len(projects) - _TOP_N_PROJECT_NAMES} more")
    projects_line = f"- Projects: {len(projects)} active"
    if project_names:
        projects_line += f" ({', '.join(project_names)})"

    agent_type_counts = dict(
        db.query(Agent.agent_type, func.count(Agent.id)).group_by(Agent.agent_type).all()
    )
    api_count = agent_type_counts.get(AgentType.API.value, 0)
    cli_count = agent_type_counts.get(AgentType.CLI.value, 0)
    default_agent = (
        db.query(Agent.model).filter(Agent.is_default.is_(True)).first()
    )
    default_model = (default_agent[0] if default_agent else None) or "none"
    agents_line = (
        f"- Agents: {api_count + cli_count} configured "
        f"({api_count} api / {cli_count} cli; default: {default_model})"
    )

    active_sessions = (
        db.query(func.count(SessionModel.id))
        .filter(SessionModel.status == SessionStatus.ACTIVE.value)
        .scalar()
        or 0
    )
    sessions_line = f"- Sessions: {active_sessions} active"

    open_status_counts = dict(
        db.query(Task.status, func.count(Task.id))
        .filter(Task.status.notin_(["done", "cancelled"]))
        .group_by(Task.status)
        .all()
    )
    open_tasks = sum(open_status_counts.values())
    dispatched = open_status_counts.get("dispatched", 0)
    in_review = open_status_counts.get("in-review", 0)
    awaiting_approval = (
        db.query(func.count(Task.id)).filter(Task.awaiting_approval.is_(True)).scalar()
        or 0
    )
    tasks_line = (
        f"- Tasks: {open_tasks} open ({dispatched} dispatched, {in_review} in-review, "
        f"{awaiting_approval} awaiting approval)"
    )

    lines = ["## System State", projects_line, agents_line, sessions_line, tasks_line]

    project_id = _scope_project_id(session, db)
    tasks_query = db.query(Task)
    if project_id:
        tasks_query = tasks_query.filter(Task.project == project_id)
    recent_tasks = (
        tasks_query.order_by(Task.updated_at.desc(), Task.id.desc()).limit(5).all()
    )
    if recent_tasks:
        header = (
            f"Recent tasks in {project_id}:" if project_id else "Recent tasks (all projects):"
        )
        lines.append(f"\n{header}")
        for task in recent_tasks:
            suffix = "" if project_id else f" [{task.project}]"
            lines.append(
                f"- {task.id}: {_clean_title(task.title)} ({task.status}){suffix}"
            )

    return "\n".join(lines)


def get_context_snapshot(session: SessionModel, db: DBSession) -> str:
    """Return a per-DB-session snapshot, rebuilding after invalidation.

    This small cache avoids repeating the same read queries when a coordinator
    retries or composes more than one provider request.  It is intentionally
    scoped to the SQLAlchemy session and is never shared across processes.
    """

    scope = _scope_project_id(session, db)
    cache = db.info.setdefault(_CACHE_INFO_KEY, {})
    if scope not in cache:
        cache[scope] = build_context_snapshot(session, db)
    return cache[scope]


def invalidate_context_snapshot(
    db: DBSession,
    project_id: str | None = None,
) -> None:
    """Invalidate snapshots affected by a project/task mutation.

    The snapshot contains the global active-project list and task counts, so
    every cached scope can be affected by a mutation.  Clearing the local
    cache gives the next chat turn a fresh, committed view of the database.
    ``project_id`` is accepted so mutation call sites can document their
    affected scope and for future narrower cache implementations.
    """

    del project_id
    cache = db.info.get(_CACHE_INFO_KEY)
    if cache is not None:
        cache.clear()


__all__ = [
    "build_context_snapshot",
    "get_context_snapshot",
    "invalidate_context_snapshot",
]
