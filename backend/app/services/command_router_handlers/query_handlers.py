import json
import sys
from typing import Any
from collections.abc import Mapping

from app.db.models import (
    Agent,
    AgentNote,
    AgentRun,
    AuditLog,
    InboxItem,
    KnowledgeItem,
    LLMUsage,
    Project,
    RunResourceUsage,
    Setting,
    Task,
    Session as SessionModel,
)
from app.db.archive import with_archived
from app.services import entity_admin
from app.services.archive import ArchiveError, ArchiveService
from app.services.embedding import EmbeddingError


_QUERY_DB_ENTITIES: dict[str, dict[str, Any]] = {
    "inbox_items": {
        "model": InboxItem,
        "filters": {"id": "str", "status": "str", "project_id": "str", "task_id": "str"},
        "order_by": InboxItem.created_at.desc(),
        "serialize": lambda i: {
            "id": i.id, "content": i.content, "project_id": i.project_id,
            "task_id": i.task_id, "tags": i.tags or [], "status": i.status,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        },
    },
    "tasks": {
        "model": Task,
        "filters": {
            "id": "str",
            "status": "str",
            "project": "str",
            "executor": "str",
            "reviewer": "str",
            "priority": "str",
            "risk": "str",
            "mode": "str",
        },
        "order_by": Task.updated_at.desc(),
        "serialize": lambda t: {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "project": t.project,
            "executor": t.executor,
            "reviewer": t.reviewer,
            "current_gate": t.current_gate,
        },
    },
    "projects": {
        "model": Project,
        "filters": {"id": "str", "status": "str"},
        "order_by": Project.id.asc(),
        "serialize": lambda p: {
            "id": p.id,
            "name": p.name,
            "status": p.status,
        },
    },
    "agents": {
        "model": Agent,
        "filters": {
            "id": "str",
            "role": "str",
            "status": "str",
            "agent_type": "str",
            "cli": "str",
            "is_default": "bool",
        },
        "order_by": Agent.id.asc(),
        "serialize": lambda a: {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "roles": a.normalized_roles,
            "status": a.status,
            "agent_type": a.agent_type,
            "model": a.model,
            "capabilities": a.normalized_capabilities,
            "is_default": a.is_default,
        },
    },
    "sessions": {
        "model": SessionModel,
        "filters": {
            "id": "str",
            "status": "str",
            "context_level": "str",
            "project_id": "str",
            "task_id": "str",
            "pinned": "bool",
        },
        "order_by": SessionModel.last_activity_at.desc(),
        "serialize": lambda s: {
            "id": s.id,
            "title": s.title,
            "status": s.status,
            "context_level": s.context_level,
            "project_id": s.project_id,
            "task_id": s.task_id,
        },
    },
    "knowledge": {
        "model": KnowledgeItem,
        "filters": {"id": "str", "category": "str", "project": "str", "author": "str"},
        "order_by": KnowledgeItem.updated_at.desc(),
        "serialize": lambda k: {
            "id": k.id,
            "title": k.title,
            "category": k.category,
            "project": k.project,
        },
    },
    "usage": {
        "model": LLMUsage,
        "filters": {
            "id": "str",
            "session_id": "str",
            "task_id": "str",
            "operation": "str",
            "model": "str",
            "provider": "str",
        },
        "order_by": LLMUsage.created_at.desc(),
        "serialize": lambda u: {
            "id": u.id,
            "model": u.model,
            "operation": u.operation,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": float(u.cost_usd) if u.cost_usd is not None else 0.0,
        },
    },
    "settings": {
        "model": Setting,
        "filters": {"id": "str"},
        "order_by": Setting.key.asc(),
        "serialize": lambda s: {
            "key": s.key,
            "value": s.value,
            "description": s.description,
        },
    },
    "agent_runs": {
        "model": AgentRun,
        "filters": {
            "id": "str",
            "task_id": "str",
            "agent_id": "str",
            "status": "str",
            "kind": "str",
        },
        "order_by": AgentRun.queued_at.desc(),
        "serialize": lambda r: {
            "id": r.id,
            "task_id": r.task_id,
            "agent_id": r.agent_id,
            "kind": r.kind,
            "status": r.status,
            "effort": r.effort,
            "exit_code": r.exit_code,
            "queued_at": r.queued_at.isoformat() if r.queued_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        },
    },
    "audit": {
        "model": AuditLog,
        "filters": {"task_id": "str", "action": "str", "actor": "str"},
        "order_by": AuditLog.created_at.desc(),
        "serialize": lambda a: {
            "id": a.id,
            "task_id": a.task_id,
            "action": a.action,
            "actor": a.actor,
            "details": a.details,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        },
    },
}


def _coerce_filter_value(raw: str, kind: str) -> Any:
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def _get_embed_text():
    cr_mod = sys.modules.get('app.services.command_router')
    if cr_mod and hasattr(cr_mod, 'embed_text'):
        return cr_mod.embed_text
    from app.services.embedding import embed_text
    return embed_text


class QueryHandlersMixin:
    @staticmethod
    def _inbox_snapshot(item: InboxItem) -> dict[str, Any]:
        return {
            'id': item.id, 'content': item.content, 'project_id': item.project_id,
            'task_id': item.task_id, 'tags': item.tags or [], 'status': item.status,
            'created_at': item.created_at.isoformat() if item.created_at else None,
            'updated_at': item.updated_at.isoformat() if item.updated_at else None,
        }

    async def _handle_manage_inbox(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid manage_inbox payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}
        action = str(payload.get('action', '')).strip().lower()
        if action not in {'add', 'update', 'delete', 'list', 'promote'}:
            return {'error': 'action must be one of add, update, delete, list, promote'}

        def validate_links(project_id, task_id):
            if project_id is not None and self.db.get(Project, project_id) is None:
                return f"Project '{project_id}' does not exist."
            if task_id is not None and self.db.get(Task, task_id) is None:
                return f"Task '{task_id}' does not exist."
            return None

        if action == 'list':
            status = payload.get('status', 'open')
            if status not in {'open', 'triaged', 'dropped'}:
                return {'error': 'status must be one of open, triaged, dropped'}
            query = self.db.query(InboxItem).filter(InboxItem.status == status)
            if payload.get('project_id') is not None:
                query = query.filter(InboxItem.project_id == payload['project_id'])
            if payload.get('q'):
                query = query.filter(InboxItem.content.ilike(f"%{payload['q']}%"))
            total = query.count()
            items = query.order_by(InboxItem.created_at.desc()).limit(50).all()
            return {'action': 'listed', 'count': total, 'items': [self._inbox_snapshot(i) for i in items]}

        item_id = str(payload.get('id', '')).strip()
        if action in {'update', 'delete', 'promote'} and not item_id:
            return {'error': 'id is required'}
        item = self.db.get(InboxItem, item_id) if item_id else None
        if action in {'update', 'delete', 'promote'} and item is None:
            return {'error': f"Inbox item '{item_id}' does not exist."}

        if action == 'add':
            content = payload.get('content')
            if not isinstance(content, str) or not content.strip():
                return {'error': 'content is required and must be non-empty'}
            project_id, task_id = payload.get('project_id'), payload.get('task_id')
            link_error = validate_links(project_id, task_id)
            if link_error:
                return {'error': link_error}
            tags = payload.get('tags', [])
            if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
                return {'error': 'tags must be an array of strings'}
            item = InboxItem(content=content, project_id=project_id, task_id=task_id, tags=tags)
            self.db.add(item)
            self.db.commit()
            return {'action': 'added', 'item': self._inbox_snapshot(item)}

        if action == 'delete':
            self.db.delete(item)
            self.db.commit()
            return {'action': 'deleted', 'id': item_id}

        if action == 'update':
            patch = payload.get('patch')
            fields = dict(patch) if isinstance(patch, Mapping) else {}
            for field in ('content', 'project_id', 'task_id', 'tags', 'status'):
                if field in payload and field not in fields:
                    fields[field] = payload[field]
            if not fields:
                return {'error': 'patch is required'}
            if 'content' in fields and (not isinstance(fields['content'], str) or not fields['content'].strip()):
                return {'error': 'content must be non-empty'}
            if 'status' in fields and fields['status'] not in {'open', 'triaged', 'dropped'}:
                return {'error': 'status must be one of open, triaged, dropped'}
            if 'tags' in fields and (not isinstance(fields['tags'], list) or not all(isinstance(tag, str) for tag in fields['tags'])):
                return {'error': 'tags must be an array of strings'}
            link_error = validate_links(fields.get('project_id', item.project_id), fields.get('task_id', item.task_id))
            if link_error:
                return {'error': link_error}
            for field in ('content', 'project_id', 'task_id', 'tags', 'status'):
                if field in fields:
                    setattr(item, field, fields[field])
            self.db.commit()
            return {'action': 'updated', 'item': self._inbox_snapshot(item)}

        project_id = payload.get('project_id') or item.project_id
        if not project_id:
            return {'error': 'project_id is required to promote an inbox item'}
        link_error = validate_links(project_id, None)
        if link_error:
            return {'error': link_error}
        title = payload.get('title') or item.content.strip().splitlines()[0][:200]
        created = await self._handle_create_task(f'{title} --project {project_id}', session_id)
        if created.get('action') != 'created':
            return created
        task = self.db.get(Task, created['task_id'])
        task.raw_input = item.content
        item.status = 'triaged'
        item.project_id = project_id
        item.task_id = task.id
        self.db.commit()
        return {'action': 'promoted', 'item': self._inbox_snapshot(item), 'task_id': task.id}

    async def _handle_query_db(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args.strip().startswith('{') else None
        except json.JSONDecodeError:
            payload = None

        if payload and 'sql' in payload:
            from app.services.sql_guard import validate_select, SQLGuardError
            from app.db.base import get_readonly_db
            from sqlalchemy import text
            
            sql = payload['sql']
            
            session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            task_id = session.task_id if session else None
            
            def log_audit(status, details):
                audit = AuditLog(
                    task_id=task_id,
                    action="query_db_sql",
                    actor=f"chat:{session_id or 'anonymous'}",
                    details={"sql": sql, "status": status, **details}
                )
                self.db.add(audit)
                self.db.commit()

            try:
                guarded_sql = validate_select(sql)
            except SQLGuardError as e:
                log_audit("rejected", {"reason": str(e)})
                return {'error': str(e), 'hint': "Chỉ chấp nhận một câu SELECT duy nhất. Xem describe schema trong tool description."}
            
            try:
                readonly_db_gen = get_readonly_db()
                readonly_db = next(readonly_db_gen)
            except RuntimeError as e:
                log_audit("error", {"reason": str(e)})
                return {'error': str(e)}
            
            try:
                readonly_db.execute(text("SET TRANSACTION READ ONLY"))
                readonly_db.execute(text("SET LOCAL statement_timeout = '10s'"))
                result = readonly_db.execute(text(guarded_sql))
                rows = [dict(row._mapping) for row in result]
                truncated = len(rows) > 500
                if truncated:
                    rows = rows[:500]
                
                log_audit("success", {"truncated": truncated, "row_count": len(rows)})
                
                resp = {
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                }
                if truncated:
                    resp["hint"] = "Kết quả bị cắt ở 500 dòng — thêm WHERE, hoặc dùng COUNT(*)/GROUP BY để tổng hợp thay vì lật trang."
                return resp
            except Exception as e:
                log_audit("error", {"reason": str(e)})
                return {'error': f"Database error: {str(e)}"}
            finally:
                readonly_db.close()

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /query <entity> [field=value ...] [limit=N] [offset=N]'}

        entity = parts[0].lower()
        entity_spec = _QUERY_DB_ENTITIES.get(entity)
        if entity_spec is None:
            return {
                'error': (
                    f"Unknown entity '{entity}'. Valid entities: "
                    f"{', '.join(sorted(_QUERY_DB_ENTITIES))}"
                )
            }

        filters: dict[str, Any] = {}
        limit = 20
        offset = 0
        include_archived = False
        for token in parts[1:]:
            if '=' not in token:
                return {'error': f"Invalid filter token '{token}', expected field=value"}
            key, _, value = token.partition('=')
            if key == 'limit':
                try:
                    limit = int(value)
                except ValueError:
                    return {'error': 'limit must be an integer'}
            elif key == 'offset':
                try:
                    offset = int(value)
                except ValueError:
                    return {'error': 'offset must be an integer'}
            elif key == 'include_archived':
                include_archived = _coerce_filter_value(value, 'bool')
            elif key in entity_spec['filters']:
                filters[key] = _coerce_filter_value(value, entity_spec['filters'][key])
            else:
                return {
                    'error': (
                        f"Unknown filter '{key}' for entity '{entity}'. "
                        f"Allowed filters: {', '.join(sorted(entity_spec['filters']))}"
                    )
                }

        if not 1 <= limit <= 50:
            return {'error': 'limit must be between 1 and 50'}
        if offset < 0:
            return {'error': 'offset must be >= 0'}

        model = entity_spec['model']
        query = with_archived(self.db, model, include_archived) if hasattr(model, 'archived_at') else self.db.query(model)
        for key, value in filters.items():
            column = getattr(model, key, None)
            if column is None and key == 'id':
                column = getattr(model, 'key', None)
            if column is not None:
                query = query.filter(column == value)
        rows = query.order_by(entity_spec['order_by']).offset(offset).limit(limit).all()

        serialized = [entity_spec['serialize'](row) for row in rows]
        if entity == 'knowledge' and 'id' in filters:
            for row, data in zip(rows, serialized):
                data['content'] = row.content

        return {
            'status': 'success',
            'entity': entity,
            'count': len(rows),
            'limit': limit,
            'offset': offset,
            'rows': serialized,
        }

    @staticmethod
    def _knowledge_snapshot(item: KnowledgeItem, distance=None) -> dict[str, Any]:
        result = {
            'id': item.id,
            'title': item.title,
            'category': item.category,
            'content': item.content,
            'tags': item.tags or [],
            'project': item.project,
            'author': item.author,
            'status': item.status,
            'created_at': item.created_at.isoformat() if item.created_at else None,
            'archived_at': item.archived_at.isoformat() if item.archived_at else None,
        }
        if distance is not None:
            result['distance'] = float(distance)
        return result

    async def _handle_manage_knowledge(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid manage_knowledge payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}

        action = str(payload.get('action', '')).strip()
        raw_id = payload.get('id')
        item_id = str(raw_id).strip() if raw_id else None
        fields = {k: v for k, v in payload.items() if k not in ('action', 'id', 'query', 'q', 'limit')}
        actor = f"chat:{session_id or 'anonymous'}"
        limit = min(100, max(1, int(payload.get('limit', 20) or 20)))

        embed_fn = _get_embed_text()

        try:
            if action == 'create':
                create_fields = dict(fields)
                if item_id:
                    create_fields['id'] = item_id
                item = entity_admin.create_knowledge(self.db, create_fields)
                # Embed content for semantic search
                content_to_embed = f"{item.title}\n{item.content}"
                try:
                    item.embedding = embed_fn(content_to_embed, self.db)
                except EmbeddingError:
                    pass  # Embedding is optional, continue without it
                self.db.flush()
            elif action == 'update':
                if not item_id:
                    return {'error': 'id is required for update'}
                item = entity_admin.update_knowledge(self.db, item_id, fields)
                # Re-embed if content or title changed
                if 'content' in fields or 'title' in fields:
                    content_to_embed = f"{item.title}\n{item.content}"
                    try:
                        item.embedding = embed_fn(content_to_embed, self.db)
                    except EmbeddingError:
                        pass
                    self.db.flush()
            elif action in {'archive', 'restore'}:
                if not item_id:
                    return {'error': f'id is required for {action}'}
                try:
                    service = ArchiveService(self.db, actor)
                    service_result = service.archive('knowledge', item_id) if action == 'archive' else service.restore('knowledge', item_id)
                except ArchiveError as exc:
                    return {'error': str(exc)}
                item = self.db.get(KnowledgeItem, item_id)
            elif action in {'list', 'search'}:
                query = str(payload.get('query') or payload.get('q') or '').strip()
                filters = [KnowledgeItem.status == 'active']
                if payload.get('project'):
                    filters.append(KnowledgeItem.project == str(payload['project']))
                if payload.get('category'):
                    filters.append(KnowledgeItem.category == str(payload['category']))
                if action == 'search' and not query and not payload.get('embedding'):
                    return {'error': 'query or embedding is required for search'}

                search_embedding = payload.get('embedding')
                if query:
                    try:
                        search_embedding = embed_fn(query, self.db)
                    except EmbeddingError as exc:
                        return {'error': f'Embedding failed: {exc}'}

                # Semantic search with pgvector
                if search_embedding and self.db.bind.dialect.name == 'postgresql':
                    from sqlalchemy import text
                    vector_value = "[" + ",".join(str(float(item)) for item in search_embedding) + "]"
                    scope_sql = ""
                    params = {'embedding': vector_value, 'limit': limit}
                    if payload.get('project'):
                        scope_sql += " AND project = :project"
                        params['project'] = str(payload['project'])
                    if payload.get('category'):
                        scope_sql += " AND category = :category"
                        params['category'] = str(payload['category'])
                    rows = self.db.execute(text(f"""
                        SELECT id, embedding <=> CAST(:embedding AS vector) AS distance
                        FROM knowledge_items
                        WHERE status = 'active' AND embedding IS NOT NULL{scope_sql}
                        ORDER BY embedding <=> CAST(:embedding AS vector)
                        LIMIT :limit
                    """), params).mappings().all()
                    ranked = {row['id']: row['distance'] for row in rows}
                    items = self.db.query(KnowledgeItem).filter(KnowledgeItem.id.in_(ranked)).all()
                    items.sort(key=lambda ki: ranked[ki.id])
                    return {'action': 'knowledge_searched', 'items': [self._knowledge_snapshot(ki, ranked[ki.id]) for ki in items]}

                # Fallback: text search or list
                statement = self.db.query(KnowledgeItem).filter(*filters)
                if query:
                    statement = statement.filter((KnowledgeItem.title.ilike(f'%{query}%')) | (KnowledgeItem.content.ilike(f'%{query}%')))
                items = statement.order_by(KnowledgeItem.updated_at.desc()).limit(limit).all()
                return {'action': 'knowledge_searched' if action == 'search' else 'knowledge_listed', 'items': [self._knowledge_snapshot(ki) for ki in items]}
            else:
                return {
                    'error': (
                        f"Unknown action '{action}'. Valid actions: "
                        "create, update, archive, restore, list, search"
                    )
                }
        except entity_admin.EntityError as exc:
            return {'error': str(exc)}

        self.db.add(
            AuditLog(
                task_id=None,
                action=f'manage_knowledge:{action}',
                actor=actor,
                details={'id': item.id, 'archive_result': locals().get('service_result')},
            )
        )
        self.db.commit()
        return {
            'action': f'knowledge_{action}d',
            'id': item.id,
            'title': item.title,
            'status': item.status,
        }

    @staticmethod
    def _note_snapshot(note: AgentNote, distance=None) -> dict[str, Any]:
        result = {
            'id': note.id,
            'title': note.title,
            'content': note.content,
            'note_type': note.note_type,
            'tags': note.tags or [],
            'project_ids': [project.id for project in note.projects],
            'task_ids': [task.id for task in note.tasks],
            'created_at': note.created_at.isoformat() if note.created_at else None,
            'archived_at': note.archived_at.isoformat() if note.archived_at else None,
        }
        if distance is not None:
            result['distance'] = float(distance)
        return result

    async def _handle_manage_notes(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid manage_notes payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}

        action = str(payload.get('action', '')).strip().lower()
        note_id = str(payload.get('id', '')).strip() or None
        limit = min(100, max(1, int(payload.get('limit', 10) or 10)))

        embed_fn = _get_embed_text()

        if action == 'save':
            title = str(payload.get('title', '')).strip()
            content = str(payload.get('content', '')).strip()
            if not title or not content:
                return {'error': 'title and content are required'}
            try:
                embedding = embed_fn(content, self.db)
            except EmbeddingError as exc:
                return {'error': f'Embedding failed: {exc}'}
            note = AgentNote(
                id=note_id or None,
                title=title,
                content=content,
                note_type=str(payload.get('note_type') or 'fact'),
                tags=payload.get('tags') or [],
                embedding=embedding,
                author=f"chat:{session_id or 'anonymous'}",
            )
            self.db.add(note)
            self.db.flush()
            project_id = str(payload.get('project_id', '')).strip() or None
            task_id = str(payload.get('task_id', '')).strip() or None
            if project_id:
                project = self.db.get(Project, project_id)
                if not project:
                    return {'error': f'Project not found: {project_id}'}
                note.projects.append(project)
            if task_id:
                task = self.db.get(Task, task_id)
                if not task:
                    return {'error': f'Task not found: {task_id}'}
                note.tasks.append(task)
            self.db.commit()
            return {'action': 'note_saved', **self._note_snapshot(note)}

        if action == 'link':
            if not note_id:
                return {'error': 'id is required for link'}
            note = self.db.get(AgentNote, note_id)
            if not note:
                return {'error': f'Note not found: {note_id}'}
            project_id = str(payload.get('project_id', '')).strip() or None
            task_id = str(payload.get('task_id', '')).strip() or None
            if not project_id and not task_id:
                return {'error': 'project_id or task_id is required for link'}
            if project_id:
                project = self.db.get(Project, project_id)
                if not project:
                    return {'error': f'Project not found: {project_id}'}
                if project not in note.projects:
                    note.projects.append(project)
            if task_id:
                task = self.db.get(Task, task_id)
                if not task:
                    return {'error': f'Task not found: {task_id}'}
                if task not in note.tasks:
                    note.tasks.append(task)
            self.db.commit()
            return {'action': 'note_linked', **self._note_snapshot(note)}

        if action == 'archive':
            if not note_id:
                return {'error': 'id is required for archive'}
            note = self.db.get(AgentNote, note_id)
            if not note:
                return {'error': f'Note not found: {note_id}'}
            note.archive()
            self.db.commit()
            return {'action': 'note_archived', **self._note_snapshot(note)}

        if action in {'list', 'search'}:
            query = str(payload.get('query') or payload.get('q') or '').strip()
            filters = [AgentNote.archived_at.is_(None)]
            if payload.get('project_id'):
                filters.append(AgentNote.projects.any(Project.id == str(payload['project_id'])))
            if payload.get('task_id'):
                filters.append(AgentNote.tasks.any(Task.id == str(payload['task_id'])))
            if action == 'search' and not query and not payload.get('embedding'):
                return {'error': 'query or embedding is required for search'}

            search_embedding = payload.get('embedding')
            if query:
                try:
                    search_embedding = embed_fn(query, self.db)
                except EmbeddingError as exc:
                    return {'error': f'Embedding failed: {exc}'}

            distance = None
            if search_embedding and self.db.bind.dialect.name == 'postgresql':
                from sqlalchemy import text
                vector_value = "[" + ",".join(str(float(item)) for item in search_embedding) + "]"
                scope_sql = ""
                params = {'embedding': vector_value, 'limit': limit}
                if payload.get('project_id'):
                    scope_sql += " AND EXISTS (SELECT 1 FROM note_projects np WHERE np.note_id = agent_notes.id AND np.project_id = :project_id)"
                    params['project_id'] = str(payload['project_id'])
                if payload.get('task_id'):
                    scope_sql += " AND EXISTS (SELECT 1 FROM note_tasks nt WHERE nt.note_id = agent_notes.id AND nt.task_id = :task_id)"
                    params['task_id'] = str(payload['task_id'])
                rows = self.db.execute(text(f"""
                    SELECT id, embedding <=> CAST(:embedding AS vector) AS distance
                    FROM agent_notes
                    WHERE archived_at IS NULL AND embedding IS NOT NULL{scope_sql}
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :limit
                """), params).mappings().all()
                ranked = {row['id']: row['distance'] for row in rows}
                notes = self.db.query(AgentNote).filter(AgentNote.id.in_(ranked)).all()
                notes.sort(key=lambda note: ranked[note.id])
                return {'action': 'notes_searched', 'notes': [self._note_snapshot(note, ranked[note.id]) for note in notes]}

            statement = self.db.query(AgentNote).filter(*filters)
            if query:
                statement = statement.filter((AgentNote.title.ilike(f'%{query}%')) | (AgentNote.content.ilike(f'%{query}%')))
            notes = statement.order_by(AgentNote.updated_at.desc()).limit(limit).all()
            return {'action': 'notes_searched' if action == 'search' else 'notes_listed', 'notes': [self._note_snapshot(note) for note in notes]}

        return {'error': "Unknown action '%s'. Valid actions: save, search, link, list, archive" % action}
