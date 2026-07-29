# CTV2-111: Soft Delete (Archive) Architecture

**Status:** Implemented (ArchivableMixin + ArchiveService)

## Overview

This document defines the soft delete (archive) architecture for CTV2. Soft delete preserves data for recovery and audit purposes while hiding archived records from normal operations.

## 1. Schema Design (AC1)

### Entities Receiving `archived_at`

| Entity | Table | Rationale |
|--------|-------|-----------|
| Task | `tasks` | Core work unit, user-created |
| Project | `projects` | Container for tasks/knowledge |
| Agent | `agents` | User-configured resource |
| KnowledgeItem | `knowledge_items` | User-created content |
| Session | `sessions` | Chat history, user-initiated |
| Setting | `settings` | System config, rare but possible |

### Entities Excluded from Soft Delete

| Entity | Table | Rationale |
|--------|-------|-----------|
| GateRecord | `gate_records` | Append-only ledger (immutable by design) |
| AdminGateRecord | `admin_gate_records` | Append-only ledger (immutable by design) |
| AuditLog | `audit_log` | Immutable audit trail |
| LLMUsage | `llm_usage` | Immutable cost ledger |
| AgentRun | `agent_runs` | Operational history, cascades with Task |
| AgentOutputChunk | `agent_output_chunks` | Part of AgentRun |
| TaskDependency | `task_dependencies` | Junction table, cascades with Task |
| ModelPricing | `model_pricing` | Reference data with effective dates |

### Column Definition

```python
archived_at = Column(DateTime(timezone=True), nullable=True, index=True)
```

- `NULL` = active/not archived
- Timestamp = archived (when it was archived)
- Indexed for efficient filtering

---

## 2. SQLAlchemy Mixin (AC3)

### ArchivableMixin

```python
# backend/app/db/mixins.py

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime
from sqlalchemy.orm import Query

class ArchivableMixin:
    """Mixin for soft-delete functionality."""
    
    archived_at = Column(DateTime(timezone=True), nullable=True, index=True)
    
    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None
    
    def archive(self) -> None:
        """Mark record as archived."""
        self.archived_at = datetime.now(timezone.utc)
    
    def restore(self) -> None:
        """Restore archived record."""
        self.archived_at = None


class ArchivableQuery(Query):
    """Custom query class that filters out archived records by default."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._include_archived = False
    
    def include_archived(self, include: bool = True) -> "ArchivableQuery":
        """Include archived records in results."""
        self._include_archived = include
        return self
    
    def archived_only(self) -> "ArchivableQuery":
        """Return only archived records."""
        return self.filter(self._entity_from_pre_ent_zero().archived_at.isnot(None))
    
    def __iter__(self):
        if not self._include_archived:
            # Apply filter only if entity has archived_at column
            entity = self._entity_from_pre_ent_zero()
            if hasattr(entity, 'archived_at'):
                self = self.filter(entity.archived_at.is_(None))
        return super().__iter__()
```

### Alternative: Query Helper Functions

A simpler approach using helper functions (recommended for SQLAlchemy 2.0):

```python
# backend/app/db/archive.py

from sqlalchemy import select
from sqlalchemy.orm import Session

def active_query(db: Session, model):
    """Return query for non-archived records."""
    return db.query(model).filter(model.archived_at.is_(None))

def with_archived(db: Session, model, include_archived: bool = False):
    """Return query with optional archived records."""
    query = db.query(model)
    if not include_archived:
        query = query.filter(model.archived_at.is_(None))
    return query

def archived_only(db: Session, model):
    """Return query for archived records only."""
    return db.query(model).filter(model.archived_at.isnot(None))
```

---

## 3. Cascade Rules (AC2)

### Cascade Hierarchy

```
Project (archived)
    ├── Task (cascade archive)
    │   ├── Session (cascade archive)
    │   └── [AgentRun, GateRecord - NOT archived, remain linked]
    └── KnowledgeItem (cascade archive)
```

### Implementation

```python
# backend/app/services/archive.py

from datetime import datetime, timezone
from sqlalchemy.orm import Session as DbSession
from app.db.models import Project, Task, KnowledgeItem, Session

class ArchiveService:
    """Service for archive/restore operations with cascade logic."""
    
    def __init__(self, db: DbSession, actor: str):
        self.db = db
        self.actor = actor
    
    def archive_project(self, project_id: str) -> dict:
        """Archive a project and all related entities."""
        now = datetime.now(timezone.utc)
        
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project {project_id} not found")
        if project.archived_at:
            raise ValueError(f"Project {project_id} already archived")
        
        # Archive in single transaction
        project.archived_at = now
        
        # Cascade to tasks
        tasks = self.db.query(Task).filter(
            Task.project == project_id,
            Task.archived_at.is_(None)
        ).all()
        for task in tasks:
            task.archived_at = now
        
        # Cascade to knowledge items
        items = self.db.query(KnowledgeItem).filter(
            KnowledgeItem.project == project_id,
            KnowledgeItem.archived_at.is_(None)
        ).all()
        for item in items:
            item.archived_at = now
        
        # Cascade to sessions
        sessions = self.db.query(Session).filter(
            Session.project_id == project_id,
            Session.archived_at.is_(None)
        ).all()
        for session in sessions:
            session.archived_at = now
        
        self.db.flush()
        
        return {
            "project": project_id,
            "tasks_archived": len(tasks),
            "knowledge_items_archived": len(items),
            "sessions_archived": len(sessions),
        }
    
    def restore_project(self, project_id: str, restore_children: bool = True) -> dict:
        """Restore an archived project and optionally its children."""
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise ValueError(f"Project {project_id} not found")
        if not project.archived_at:
            raise ValueError(f"Project {project_id} is not archived")
        
        archived_at = project.archived_at
        project.archived_at = None
        
        result = {"project": project_id, "tasks_restored": 0, "knowledge_items_restored": 0}
        
        if restore_children:
            # Restore tasks that were archived at the same time as project
            tasks = self.db.query(Task).filter(
                Task.project == project_id,
                Task.archived_at == archived_at
            ).all()
            for task in tasks:
                task.archived_at = None
            result["tasks_restored"] = len(tasks)
            
            # Restore knowledge items
            items = self.db.query(KnowledgeItem).filter(
                KnowledgeItem.project == project_id,
                KnowledgeItem.archived_at == archived_at
            ).all()
            for item in items:
                item.archived_at = None
            result["knowledge_items_restored"] = len(items)
            
            # Restore sessions
            sessions = self.db.query(Session).filter(
                Session.project_id == project_id,
                Session.archived_at == archived_at
            ).all()
            for session in sessions:
                session.archived_at = None
            result["sessions_restored"] = len(sessions)
        
        self.db.flush()
        return result
```

### Cascade Matrix

| Parent Action | Child Entity | Behavior |
|--------------|--------------|----------|
| Archive Project | Task | Archive (same timestamp) |
| Archive Project | KnowledgeItem | Archive (same timestamp) |
| Archive Project | Session | Archive (same timestamp) |
| Archive Task | Session | Archive (same timestamp) |
| Restore Project | Task | Restore if archived_at matches |
| Restore Project | KnowledgeItem | Restore if archived_at matches |
| Restore Task | Session | Restore if archived_at matches |

---

## 4. Tool Design (AC4)

### Updates to Existing Manage Tools

The existing `manage_project`, `manage_agent`, and similar tools need new actions:

```python
# In tool definitions (e.g., coordinator_tools.py)

# manage_project tool - add actions
ProjectAction = Literal["get", "list", "create", "update", "delete", "archive", "restore"]

# manage_agent tool - add actions  
AgentAction = Literal["get", "list", "create", "update", "delete", "archive", "restore"]

# manage_knowledge tool - add actions
KnowledgeAction = Literal["get", "list", "create", "update", "delete", "archive", "restore"]
```

### Tool Permission

Archive/restore operations require `admin` permission level. This is enforced via the existing admin gate pattern:

```python
# In entity_admin.py service

ARCHIVE_ACTIONS = {"archive", "restore"}

async def handle_manage_request(action: str, entity: str, ...):
    if action in ARCHIVE_ACTIONS:
        # Routes through AdminGateRecord for approval
        # Same as create/update/delete in supervised mode
        pass
```

### Example Tool Invocation

```json
{
  "tool": "manage_project",
  "params": {
    "action": "archive",
    "id": "my-project"
  }
}
```

Response:
```json
{
  "status": "archived",
  "project": "my-project",
  "cascade_summary": {
    "tasks_archived": 15,
    "knowledge_items_archived": 8,
    "sessions_archived": 3
  }
}
```

---

## 5. API Design (AC5)

### Query Parameter

All list endpoints support `?include_archived=true`:

```python
@router.get("", response_model=list[Task])
def get_tasks(
    status: str | None = None,
    project: str | None = None,
    include_archived: bool = Query(False, description="Include archived tasks"),
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(TaskModel)
    
    # Default: exclude archived
    if not include_archived:
        query = query.filter(TaskModel.archived_at.is_(None))
    
    # ... rest of filtering
```

### Archive/Restore Endpoints

```python
# POST /api/projects/{id}/archive
@router.post("/{project_id}/archive")
def archive_project(
    project_id: str,
    actor: str = Query(...),
    db: Session = Depends(get_db)
):
    service = ArchiveService(db, actor)
    result = service.archive_project(project_id)
    
    # Log to audit
    log_archive_action(db, "archive", "project", project_id, actor, result)
    
    db.commit()
    return result

# POST /api/projects/{id}/restore
@router.post("/{project_id}/restore")
def restore_project(
    project_id: str,
    actor: str = Query(...),
    restore_children: bool = Query(True),
    db: Session = Depends(get_db)
):
    service = ArchiveService(db, actor)
    result = service.restore_project(project_id, restore_children)
    
    # Log to audit
    log_archive_action(db, "restore", "project", project_id, actor, result)
    
    db.commit()
    return result
```

### Archived-Only Filter

For admin views that show only archived items:

```
GET /api/tasks?archived_only=true
```

---

## 6. Audit Logging (AC6)

### Audit Log Entries

Archive/restore actions are logged to the existing `audit_log` table:

```python
def log_archive_action(
    db: Session,
    action: str,  # "archive" or "restore"
    entity_type: str,  # "project", "task", "agent", etc.
    entity_id: str,
    actor: str,
    details: dict | None = None
):
    log = AuditLogModel(
        task_id=entity_id if entity_type == "task" else None,
        action=f"{entity_type}_{action}",  # e.g., "project_archive"
        actor=actor,
        details={
            "entity_type": entity_type,
            "entity_id": entity_id,
            "cascade_summary": details,
        }
    )
    db.add(log)
```

### Audit Log Schema Extension

The existing `AuditLog` model already supports this via the `details` JSON column. Example log entry:

```json
{
  "id": 1234,
  "task_id": null,
  "action": "project_archive",
  "actor": "user@example.com",
  "details": {
    "entity_type": "project",
    "entity_id": "my-project",
    "cascade_summary": {
      "tasks_archived": 15,
      "knowledge_items_archived": 8,
      "sessions_archived": 3
    }
  },
  "created_at": "2026-07-28T10:00:00Z"
}
```

---

## 7. Migration Strategy (AC7)

### Alembic Migration

```python
"""Add archived_at column to archivable entities

Revision ID: xxxx
Revises: yyyy
"""

from alembic import op
import sqlalchemy as sa

def upgrade():
    # Add archived_at to all archivable tables
    for table in ['tasks', 'projects', 'agents', 'knowledge_items', 'sessions', 'settings']:
        op.add_column(
            table,
            sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index(
            f'ix_{table}_archived_at',
            table,
            ['archived_at']
        )

def downgrade():
    for table in ['tasks', 'projects', 'agents', 'knowledge_items', 'sessions', 'settings']:
        op.drop_index(f'ix_{table}_archived_at', table_name=table)
        op.drop_column(table, 'archived_at')
```

### Rollout Plan

1. **Phase 1: Schema Migration**
   - Deploy migration adding `archived_at` columns
   - All existing records have `NULL` (active) by default
   - No breaking changes to existing queries

2. **Phase 2: Query Updates**
   - Update all list queries to filter `WHERE archived_at IS NULL` by default
   - Add `include_archived` query parameter support
   - Deploy to all API endpoints

3. **Phase 3: Tool/UI Updates**
   - Add archive/restore actions to manage tools
   - Add archive/restore buttons to dashboard
   - Add "show archived" toggle to list views

### Backward Compatibility

- `NULL` default means existing data is automatically "active"
- Queries without filter behave as before (return all active records)
- No FK changes required
- No data migration needed

---

## 8. Edge Cases

### Archiving a Task with Active Session

- Session is archived along with Task
- If session is in use, archive still proceeds (session shows as archived in UI)
- User can continue viewing archived session read-only

### Archiving an Agent

- Agents are not cascaded from anywhere (top-level entity)
- Archived agent cannot be assigned to new tasks
- Existing tasks with archived agent can still complete

### Foreign Key References to Archived Entities

- FKs remain valid (no constraint violations)
- Queries filtering archived_at don't break relationships
- Example: `Task.project` can reference an archived Project (historical integrity)

### Restoring a Child Without Parent

- Not allowed: restoring a Task requires its Project to be active
- Validation: `if project.archived_at is not None: raise Error`

---

## 9. Summary

| AC | Requirement | Solution |
|----|-------------|----------|
| AC1 | Schema design | `archived_at` (nullable timestamp) on 6 entities |
| AC2 | Cascade rules | ArchiveService with same-transaction cascades |
| AC3 | Query filtering | Default `WHERE archived_at IS NULL`, helper functions |
| AC4 | Tool design | `archive`/`restore` actions, admin permission |
| AC5 | API design | `?include_archived=true` query param |
| AC6 | Audit | Logged to `audit_log` with entity_type, entity_id, actor |
| AC7 | Migration | Single Alembic migration, no data changes |

---

## 10. Implementation Checklist

- [ ] Create `backend/app/db/mixins.py` with ArchivableMixin
- [ ] Create `backend/app/db/archive.py` with helper functions
- [ ] Create `backend/app/services/archive.py` with ArchiveService
- [ ] Update models to include ArchivableMixin
- [ ] Create Alembic migration
- [ ] Update all list API endpoints with `include_archived` param
- [ ] Add archive/restore endpoints to each entity router
- [ ] Update manage tools with archive/restore actions
- [ ] Add audit logging for archive/restore
- [ ] Update dashboard with archive UI
- [ ] Write tests for cascade behavior
