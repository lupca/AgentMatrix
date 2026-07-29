from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Agent, AuditLog, KnowledgeItem, Project, Session as ChatSession
from app.db.models import Setting, Task


class ArchiveError(ValueError):
    pass


_MODELS = {
    "task": Task,
    "tasks": Task,
    "project": Project,
    "projects": Project,
    "agent": Agent,
    "agents": Agent,
    "knowledge": KnowledgeItem,
    "knowledge_item": KnowledgeItem,
    "knowledge_items": KnowledgeItem,
    "session": ChatSession,
    "sessions": ChatSession,
    "setting": Setting,
    "settings": Setting,
}


class ArchiveService:
    """Archive and restore entities while preserving immutable history."""

    def __init__(self, db: Session, actor: str = "system"):
        self.db = db
        self.actor = actor

    def archive(self, entity: str, entity_id: str) -> dict[str, Any]:
        model = self._model(entity)
        obj = self._get(model, entity_id)
        if obj.is_archived:
            raise ArchiveError(f"{entity} '{entity_id}' is already archived")
        archived_at = datetime.now(timezone.utc)
        obj.archived_at = archived_at
        # Also set status to "archived" if the entity has a status field
        if hasattr(obj, "status"):
            obj.status = "archived"
        result = {"entity": entity, "id": entity_id, "action": "archive", "archived": 1}
        if model is Project:
            result.update(self._cascade_project(entity_id, archived_at))
        elif model is Task:
            result.update(self._cascade_task(entity_id, archived_at))
        self._audit(entity, entity_id, "archive", result)
        self.db.commit()
        return result

    def restore(self, entity: str, entity_id: str, restore_children: bool = True) -> dict[str, Any]:
        model = self._model(entity)
        obj = self._get(model, entity_id)
        if not obj.is_archived:
            raise ArchiveError(f"{entity} '{entity_id}' is not archived")
        archived_at = obj.archived_at
        obj.archived_at = None
        # Also restore status to "active" if the entity has a status field
        if hasattr(obj, "status"):
            obj.status = "active"
        result = {"entity": entity, "id": entity_id, "action": "restore", "restored": 1}
        if restore_children and model is Project:
            result.update(self._restore_project(entity_id, archived_at))
        elif restore_children and model is Task:
            result.update(self._restore_task(entity_id, archived_at))
        self._audit(entity, entity_id, "restore", result)
        self.db.commit()
        return result

    def archive_project(self, project_id: str) -> dict[str, Any]:
        return self.archive("projects", project_id)

    def restore_project(self, project_id: str, restore_children: bool = True) -> dict[str, Any]:
        return self.restore("projects", project_id, restore_children)

    def _model(self, entity: str):
        try:
            return _MODELS[entity.strip().lower()]
        except KeyError as exc:
            raise ArchiveError(f"Unknown archivable entity '{entity}'") from exc

    def _get(self, model, entity_id: str):
        obj = self.db.get(model, entity_id)
        if obj is None:
            raise ArchiveError(f"{model.__tablename__} '{entity_id}' not found")
        return obj

    def _cascade_project(self, project_id: str, archived_at: datetime) -> dict[str, int]:
        tasks = self.db.query(Task).filter(Task.project == project_id, Task.archived_at.is_(None)).all()
        items = self.db.query(KnowledgeItem).filter(KnowledgeItem.project == project_id, KnowledgeItem.archived_at.is_(None)).all()
        sessions = self.db.query(ChatSession).filter(ChatSession.project_id == project_id, ChatSession.archived_at.is_(None)).all()
        for child in [*tasks, *items, *sessions]:
            child.archived_at = archived_at
        return {"tasks_archived": len(tasks), "knowledge_items_archived": len(items), "sessions_archived": len(sessions)}

    def _cascade_task(self, task_id: str, archived_at: datetime) -> dict[str, int]:
        sessions = self.db.query(ChatSession).filter(ChatSession.task_id == task_id, ChatSession.archived_at.is_(None)).all()
        for session in sessions:
            session.archived_at = archived_at
        return {"sessions_archived": len(sessions)}

    def _restore_project(self, project_id: str, archived_at: datetime) -> dict[str, int]:
        tasks = self.db.query(Task).filter(Task.project == project_id, Task.archived_at == archived_at).all()
        items = self.db.query(KnowledgeItem).filter(KnowledgeItem.project == project_id, KnowledgeItem.archived_at == archived_at).all()
        sessions = self.db.query(ChatSession).filter(ChatSession.project_id == project_id, ChatSession.archived_at == archived_at).all()
        for child in [*tasks, *items, *sessions]:
            child.archived_at = None
        return {"tasks_restored": len(tasks), "knowledge_items_restored": len(items), "sessions_restored": len(sessions)}

    def _restore_task(self, task_id: str, archived_at: datetime) -> dict[str, int]:
        sessions = self.db.query(ChatSession).filter(ChatSession.task_id == task_id, ChatSession.archived_at == archived_at).all()
        for session in sessions:
            session.archived_at = None
        return {"sessions_restored": len(sessions)}

    def _audit(self, entity: str, entity_id: str, action: str, result: dict[str, Any]) -> None:
        self.db.add(AuditLog(task_id=entity_id if entity in {"task", "tasks"} else None,
                             action=f"{action}:{entity}", actor=self.actor,
                             details={"entity": entity, "entity_id": entity_id, **result}))
