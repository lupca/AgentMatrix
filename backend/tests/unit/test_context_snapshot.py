from datetime import datetime, timezone

from app.db.models import Project, Session, Task
from app.graph.context import (
    build_context_snapshot,
    get_context_snapshot,
    invalidate_context_snapshot,
)


def test_snapshot_lists_active_projects_with_aggregate_task_counts(db_session):
    db_session.add_all(
        [
            Project(id="alpha", name="Alpha", status="active"),
            Project(id="archived", name="Archived", status="archived"),
            Task(id="ALPHA-001", project="alpha", title="First", status="todo"),
            Task(id="ALPHA-002", project="alpha", title="Second", status="dispatched"),
        ]
    )
    db_session.commit()
    session = Session(id="global-snapshot", messages=[])
    db_session.add(session)
    db_session.commit()

    snapshot = build_context_snapshot(session, db_session)

    assert "Projects (1):" in snapshot
    assert "- alpha: Alpha (active, 2 tasks)" in snapshot
    assert "archived" not in snapshot


def test_snapshot_includes_only_five_recent_tasks_for_project_scope(db_session):
    db_session.add(Project(id="alpha", name="Alpha", status="active"))
    for number in range(7):
        db_session.add(
            Task(
                id=f"ALPHA-{number:03d}",
                project="alpha",
                title=f"Task {number}",
                status="dispatched" if number == 6 else "todo",
                updated_at=datetime(2026, 7, number + 1, tzinfo=timezone.utc),
            )
        )
    db_session.commit()
    session = Session(
        id="project-snapshot",
        project_id="alpha",
        context_level="project",
        messages=[],
    )
    db_session.add(session)
    db_session.commit()

    snapshot = build_context_snapshot(session, db_session)

    assert "Recent tasks in alpha:" in snapshot
    assert "ALPHA-006: Task 6 (dispatched)" in snapshot
    assert "ALPHA-001" not in snapshot
    assert snapshot.count("- ALPHA-") == 5


def test_empty_snapshot_is_stable_and_human_readable(db_session):
    session = Session(id="empty-snapshot", messages=[])
    db_session.add(session)
    db_session.commit()

    assert build_context_snapshot(session) == "## Current Context\nProjects (0):"


def test_mutation_invalidation_refreshes_cached_snapshot(db_session):
    db_session.add(Project(id="alpha", name="Alpha", status="active"))
    db_session.commit()
    session = Session(id="cached-snapshot", messages=[])
    db_session.add(session)
    db_session.commit()

    assert "Alpha" in get_context_snapshot(session, db_session)
    db_session.get(Project, "alpha").name = "Renamed Alpha"
    db_session.commit()

    # The endpoint mutation hooks call this same invalidation function.
    invalidate_context_snapshot(db_session, project_id="alpha")
    assert "Renamed Alpha" in get_context_snapshot(session, db_session)
