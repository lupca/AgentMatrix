from datetime import datetime, timezone

from app.db.models import Project, Session, Task
from app.graph.context import (
    build_context_snapshot,
    get_context_snapshot,
    invalidate_context_snapshot,
)


def test_snapshot_lists_active_projects_and_open_task_breakdown(db_session):
    db_session.add_all(
        [
            Project(id="alpha", name="Alpha", status="active"),
            Project(id="archived", name="Archived", status="archived"),
            Task(id="ALPHA-001", project="alpha", title="First", status="todo"),
            Task(id="ALPHA-002", project="alpha", title="Second", status="dispatched"),
            Task(id="ALPHA-003", project="alpha", title="Third", status="in-review"),
            Task(
                id="ALPHA-004",
                project="alpha",
                title="Fourth",
                status="dispatched",
                awaiting_approval=True,
            ),
        ]
    )
    db_session.commit()
    session = Session(id="global-snapshot", messages=[])
    db_session.add(session)
    db_session.commit()

    snapshot = build_context_snapshot(session, db_session)

    assert "## System State" in snapshot
    assert "- Projects: 1 active (Alpha)" in snapshot
    assert "archived" not in snapshot
    assert "- Tasks: 4 open (2 dispatched, 1 in-review, 1 awaiting approval)" in snapshot


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

    # The session row itself is active, so it counts toward "Sessions".
    assert build_context_snapshot(session) == (
        "## System State\n"
        "- Projects: 0 active\n"
        "- Agents: 0 configured (0 api / 0 cli; default: none)\n"
        "- Sessions: 1 active\n"
        "- Tasks: 0 open (0 dispatched, 0 in-review, 0 awaiting approval)"
    )


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
