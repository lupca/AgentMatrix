from app.db.archive import active_query, archived_only, with_archived
from app.db.models import AuditLog, KnowledgeItem, Project, Session, Task
from app.services.archive import ArchiveService


def test_project_archive_cascades_and_restore_is_timestamp_scoped(db_session):
    project = Project(id="archive-project", name="Archive project")
    task = Task(id="ARCH-001", project=project.id, title="task")
    session = Session(
        id="archive-session",
        project_id=project.id,
        task_id=task.id,
        context_level="task",
    )
    item = KnowledgeItem(
        id="archive-item", title="item", content="content", project=project.id
    )
    db_session.add_all([project, task, session, item])
    db_session.commit()

    result = ArchiveService(db_session, "test-user").archive_project(project.id)
    assert result["tasks_archived"] == 1
    assert result["sessions_archived"] == 1
    assert active_query(db_session, Task).count() == 0
    assert archived_only(db_session, Task).count() == 1
    assert with_archived(db_session, Task, True).count() == 1

    # A child archived independently must not be restored by the project restore.
    db_session.query(KnowledgeItem).filter_by(id=item.id).update({"archived_at": None})
    db_session.commit()
    ArchiveService(db_session, "test-user").restore_project(project.id)
    assert task.archived_at is None
    assert session.archived_at is None
    assert db_session.get(KnowledgeItem, item.id).archived_at is None
    assert db_session.query(AuditLog).filter(AuditLog.action == "archive:projects").count() == 1
    assert db_session.query(AuditLog).filter(AuditLog.action == "restore:projects").count() == 1


def test_archive_is_idempotency_safe(db_session):
    db_session.add(Project(id="once", name="Once"))
    db_session.commit()
    service = ArchiveService(db_session)
    service.archive("projects", "once")
    try:
        service.archive("projects", "once")
    except ValueError as exc:
        assert "already archived" in str(exc)
    else:
        raise AssertionError("archiving an archived entity must fail")
