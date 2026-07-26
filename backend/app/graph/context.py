"""Compact, structured context used by the user-chat coordinator.

The snapshot is deliberately built from relational data instead of chat
history.  That keeps read questions cheap while mutations continue to be
handled by the coordinator's tools/commands.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession, object_session

from app.db.models import Project, Session as SessionModel, Task


_CACHE_INFO_KEY = "control_tower_context_snapshot_cache"


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
    """Generate a compact, human-readable summary of current project state.

    Active projects are listed with task counts.  When the session has a
    project scope, the five most recently updated tasks in that project are
    included as well.  ``db`` is optional for attached ORM instances, which
    keeps this function convenient in unit tests while allowing callers to
    pass an explicit database session.
    """

    db = _database_for(session, db)
    projects = (
        db.query(Project)
        .filter(Project.status == "active")
        .order_by(Project.id.asc())
        .all()
    )

    task_counts: dict[str, int] = {}
    if projects:
        counts = (
            db.query(Task.project, func.count(Task.id))
            .filter(Task.project.in_([project.id for project in projects]))
            .group_by(Task.project)
            .all()
        )
        task_counts = {project_id: int(count) for project_id, count in counts}

    lines = ["## Current Context", f"Projects ({len(projects)}):"]
    for project in projects:
        lines.append(
            f"- {project.id}: {project.name} ({project.status}, "
            f"{task_counts.get(project.id, 0)} tasks)"
        )

    project_id = _scope_project_id(session, db)
    if project_id:
        recent_tasks = (
            db.query(Task)
            .filter(Task.project == project_id)
            .order_by(Task.updated_at.desc(), Task.id.desc())
            .limit(5)
            .all()
        )
        lines.append(f"\nRecent tasks in {project_id}:")
        for task in recent_tasks:
            lines.append(
                f"- {task.id}: {_clean_title(task.title)} ({task.status})"
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
