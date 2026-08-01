# Implementation Plan: Project Context & Scoped Rules

## Mục tiêu

1. **Giảm token ~40%** - Agent hiểu đúng từ đầu, ít retry
2. **Tăng chất lượng** - Rules phù hợp với files đang edit

## Tổng quan

```
┌─────────────────────────────────────────────────────────────┐
│  User tạo task / trigger dispatch                           │
│      ↓                                                      │
│  [Context Check] ─── empty? ──→ [Context Gen Dispatch]      │
│      ↓                                │                     │
│      ↓ có context                     ↓                     │
│      ↓                          Agent scan repo             │
│      ↓                          Agent gọi MCP tool          │
│      ↓                          save_project_context()      │
│      ↓                          → agenticmatix API → DB     │
│      ↓ ←──────────────────────────────┘                     │
│  [Load Scoped Rules] ─── match task.files với globs         │
│      ↓                                                      │
│  [Inject vào Prompt]                                        │
│      ↓                                                      │
│  [Dispatch Agent]                                           │
└─────────────────────────────────────────────────────────────┘
```

## Cơ chế lưu context (MCP Tool)

Agent dispatch vào target project **KHÔNG cần** target project có gì đặc biệt.

agenticmatix tự inject MCP server khi dispatch:

```python
# cli_dispatcher.py - đã có sẵn
build_mcp_config(api_url, token) → {
    "mcpServers": {
        "control-tower": {
            "command": "python -m app.mcp_server",
            "args": ["--api-url", api_url],
            "env": {"CT_MCP_TOKEN": token}
        }
    }
}
```

Agent gọi MCP tool → MCP server forward → agenticmatix API → Save DB.

---

## Phase 1: Database Schema

### 1.1 Migration: Add ProjectRule table

```python
# backend/alembic/versions/xxx_add_project_rules.py

def upgrade():
    op.create_table(
        'project_rules',
        sa.Column('id', sa.String(50), primary_key=True),
        sa.Column('project_id', sa.String(50), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('globs', sa.JSON, nullable=False, default=[]),  # ["backend/app/schemas/**/*.py"]
        sa.Column('content', sa.Text, nullable=False),
        sa.Column('priority', sa.Integer, nullable=False, default=0),  # Higher = load first
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), onupdate=func.now()),
        sa.UniqueConstraint('project_id', 'name', name='uq_project_rules_project_name'),
    )
    op.create_index('ix_project_rules_project_id', 'project_rules', ['project_id'])

def downgrade():
    op.drop_table('project_rules')
```

### 1.2 Model: ProjectRule

```python
# backend/app/db/models.py

class ProjectRule(Base):
    __tablename__ = "project_rules"

    id = Column(String(50), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(50), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    globs = Column(JSON, nullable=False, default=list)  # ["backend/**/*.py"]
    content = Column(Text, nullable=False)
    priority = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    project = relationship("Project", back_populates="rules")

    __table_args__ = (
        UniqueConstraint('project_id', 'name', name='uq_project_rules_project_name'),
    )
```

### 1.3 Update Project model

```python
# backend/app/db/models.py - Project class

class Project(Base):
    # ... existing fields ...
    
    # Add relationship
    rules = relationship("ProjectRule", back_populates="project", cascade="all, delete-orphan")
    
    # Add flag for context generation status
    context_generated = Column(Boolean, nullable=False, default=False)
```

---

## Phase 2: MCP Tool + Context Service

### 2.1 Thêm MCP Tool: save_project_context

```python
# backend/app/services/tool_registry.py - Thêm vào TOOL_REGISTRY

ToolSpec(
    name="save_project_context",
    description="Save generated context and rules for a project. Call this after scanning the codebase.",
    parameters={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "Project ID to save context for"
            },
            "context_md": {
                "type": "string",
                "description": "Project context markdown (≤150 lines). Include: Stack, Hard Boundaries, Key Patterns"
            },
            "rules": {
                "type": "array",
                "description": "Scoped rules with glob patterns (max 5 rules)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Rule name (e.g., 'architecture', 'schemas')"},
                        "globs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File patterns this rule applies to (e.g., ['backend/**/*.py'])"
                        },
                        "content": {"type": "string", "description": "Rule content (≤30 lines)"}
                    },
                    "required": ["name", "content"]
                }
            }
        },
        "required": ["project_id", "context_md"]
    },
    group="admin",
)
```

### 2.2 Handler trong CommandRouter

```python
# backend/app/services/command_router.py - Thêm handler

async def _handle_save_project_context(self, args: dict) -> dict:
    """Handle save_project_context MCP tool call."""
    project_id = args["project_id"]
    project = self.db.get(Project, project_id)
    
    if not project:
        return {"error": f"Project {project_id} not found"}
    
    # Validate context length
    context_md = args["context_md"]
    if len(context_md.split('\n')) > 150:
        return {"error": "context_md exceeds 150 lines limit"}
    
    # Save context_md
    project.context_md = context_md
    project.context_generated = True
    
    # Clear existing rules and save new ones
    self.db.query(ProjectRule).filter_by(project_id=project_id).delete()
    
    rules_data = args.get("rules", [])
    for rule_data in rules_data[:5]:  # Max 5 rules
        rule = ProjectRule(
            id=str(uuid.uuid4()),
            project_id=project_id,
            name=rule_data["name"],
            globs=rule_data.get("globs", []),
            content=rule_data["content"][:3000],  # Max content length
        )
        self.db.add(rule)
    
    self.db.commit()
    
    return {
        "status": "saved",
        "project_id": project_id,
        "context_lines": len(context_md.split('\n')),
        "rules_count": len(rules_data),
    }
```

### 2.3 Context Check Service

```python
# backend/app/services/context_generator.py

from fnmatch import fnmatch
from sqlalchemy.orm import Session
from app.db.models import Project, ProjectRule


class ContextChecker:
    """Check if project has context ready, used before dispatch."""
    
    def __init__(self, db: Session):
        self.db = db

    def check_project_ready(self, project_id: str) -> dict[str, bool]:
        """Check if project has required context."""
        project = self.db.get(Project, project_id)
        if not project:
            return {"exists": False, "ready": False}
        
        rules = self.db.query(ProjectRule).filter_by(project_id=project_id).all()
        
        return {
            "exists": True,
            "has_context": bool(project.context_md),
            "has_rules": len(rules) > 0,
            "context_generated": getattr(project, 'context_generated', False),
            "ready": bool(project.context_md) and len(rules) > 0,
        }


def get_matching_rules(
    db: Session,
    project_id: str,
    task_files: list[str] | None,
) -> list[ProjectRule]:
    """Get rules matching task files using glob patterns."""
    rules = (
        db.query(ProjectRule)
        .filter(ProjectRule.project_id == project_id)
        .order_by(ProjectRule.priority.desc())
        .all()
    )
    
    if not task_files:
        # No files specified → return all rules
        return rules
    
    matched = []
    for rule in rules:
        if not rule.globs:
            # No globs = applies to all files
            matched.append(rule)
            continue
        
        for task_file in task_files:
            if any(fnmatch(task_file, glob) for glob in rule.globs):
                matched.append(rule)
                break
    
    return matched
```

### 2.4 Context Gen Task Prompt

```python
# backend/app/services/context_generator.py

CONTEXT_GEN_PROMPT = '''
You are generating project context for an AI coding assistant system.

Scan this repository and then call the `save_project_context` tool with:

1. **context_md** (≤150 lines total):
```markdown
# Project: {project_name}

## Stack
One line describing tech stack (e.g., "FastAPI + SQLAlchemy + React")

## Hard Boundaries (max 7 rules)
- Critical rules that MUST NOT be violated
- Example: "NEVER use db.commit() directly, use db.flush() + db.refresh()"

## Key Patterns (max 5)
- Common patterns in this codebase
- Example: "Routes → Services → Repositories pattern"
```

2. **rules** (max 5 scoped rules):
Each rule should have:
- name: short identifier (e.g., "architecture", "schemas", "api")
- globs: file patterns it applies to (e.g., ["backend/app/schemas/**/*.py"])
- content: the rule details (≤30 lines)

IMPORTANT:
- Keep EVERYTHING concise - bloated context hurts agent performance
- Do NOT document file structure (gets stale quickly)
- Focus on conventions and constraints, not descriptions
- Hard boundaries = things that will break if violated

After scanning, call `save_project_context` with project_id="{project_id}".
'''
```

---

## Phase 3: Integrate with Dispatch Flow

### 3.1 Update command_builder.py

```python
# backend/app/services/command_builder.py

from app.services.context_generator import get_matching_rules

def build_dispatch_command(
    task: Task,
    agent: Agent,
    project: Optional[Project] = None,
    effort: Optional[str] = None,
    db: Optional[Session] = None,  # Add db parameter
) -> tuple[str, str, str]:
    # ... existing validation code ...

    prompt = _task_prompt(task, review_result_path(repo_root, task.id))
    
    # NEW: Inject project context
    if project and project.context_md:
        prompt = f"[Project Context]\n{project.context_md}\n\n{prompt}"
    
    # NEW: Inject matching rules
    if db and project:
        rules = get_matching_rules(db, project.id, task.files)
        if rules:
            rules_text = "\n\n".join(
                f"## {r.name}\n{r.content}" for r in rules
            )
            prompt = f"[Project Rules]\n{rules_text}\n\n{prompt}"
    
    # ... rest of existing code ...
```

### 3.2 Update task_orchestration.py - Add context check before dispatch

```python
# backend/app/services/task_orchestration.py

from app.services.context_generator import ContextChecker, CONTEXT_GEN_PROMPT

class TaskOrchestrationService:
    # ... existing code ...

    def request_dispatch(
        self,
        *,
        task_id: str,
        agent_id: str,
        actor: str,
        idempotency_key: str,
        # ... other params ...
    ) -> TransitionResult:
        task = self._task(task_id)
        project = self.db.get(Project, task.project)
        
        # NEW: Check context ready
        checker = ContextChecker(self.db)
        context_check = checker.check_project_ready(project.id)
        
        if not context_check["ready"]:
            # Get autonomy mode
            mode = self._get_autonomy_mode(project)
            
            if mode == "bypass":
                # Auto-trigger context generation dispatch
                return self._trigger_context_gen_dispatch(
                    task=task,
                    project=project,
                    agent_id=agent_id,
                    actor=actor,
                    original_idempotency_key=idempotency_key,
                )
            else:
                # Supervised mode - return info for coordinator to ask user
                raise PrerequisiteError(
                    f"Project {project.id} missing context. "
                    f"Missing: {context_check}. "
                    "Run context generation first or ask user to approve."
                )
        
        # ... rest of existing dispatch code ...

    def _trigger_context_gen_dispatch(
        self,
        task: Task,
        project: Project,
        agent_id: str,
        actor: str,
        original_idempotency_key: str,
    ) -> TransitionResult:
        """Dispatch a context generation task before the original task.
        
        Agent sẽ:
        1. Scan target repo
        2. Gọi MCP tool save_project_context()
        3. MCP forward về agenticmatix API
        4. Save context + rules vào DB
        """
        # Create context gen task
        context_task = Task(
            id=f"{project.id}-context-gen",
            project=project.id,
            title=f"Generate context for {project.name}",
            status="todo",
            raw_input=CONTEXT_GEN_PROMPT.format(
                project_name=project.name,
                project_id=project.id,
            ),
            acceptance_criteria=[
                "Scan repository structure and conventions",
                "Generate context_md (≤150 lines) with Stack, Hard Boundaries, Key Patterns",
                "Generate scoped rules with globs (max 5 rules)",
                "Call save_project_context tool to save to database",
            ],
        )
        self.db.add(context_task)
        self.db.flush()
        
        # Dispatch context gen task immediately (bypass gate since this is auto-triggered)
        # After context gen completes, original task can be retried
        
        return TransitionResult(
            gate_record=self._create_context_gen_gate(context_task, task),
            context={
                "action": "context_gen_triggered",
                "context_task_id": context_task.id,
                "original_task_id": task.id,
                "message": "Context generation dispatched. Original task will resume after completion.",
            }
        )
```

---

## Phase 4: API Endpoints

### 4.1 Project Rules CRUD

```python
# backend/app/api/routes/project_rules.py

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.db.models import ProjectRule
from app.schemas.project_rule import (
    ProjectRuleCreate,
    ProjectRuleUpdate,
    ProjectRuleRead,
)

router = APIRouter(prefix="/projects/{project_id}/rules", tags=["project-rules"])


@router.get("", response_model=list[ProjectRuleRead])
def list_rules(project_id: str, db: Session = Depends(get_db)):
    return db.query(ProjectRule).filter_by(project_id=project_id).all()


@router.post("", response_model=ProjectRuleRead, status_code=201)
def create_rule(
    project_id: str,
    data: ProjectRuleCreate,
    db: Session = Depends(get_db),
):
    rule = ProjectRule(
        project_id=project_id,
        **data.model_dump(),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/{rule_id}", response_model=ProjectRuleRead)
def update_rule(
    project_id: str,
    rule_id: str,
    data: ProjectRuleUpdate,
    db: Session = Depends(get_db),
):
    rule = db.query(ProjectRule).filter_by(id=rule_id, project_id=project_id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=204)
def delete_rule(
    project_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
):
    rule = db.query(ProjectRule).filter_by(id=rule_id, project_id=project_id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    
    db.delete(rule)
    db.commit()
```

### 4.2 Context Generation Endpoint

```python
# backend/app/api/routes/projects.py - Add to existing

@router.post("/{project_id}/generate-context")
async def generate_context(
    project_id: str,
    agent_id: str = Query(..., description="Agent to use for generation"),
    db: Session = Depends(get_db),
):
    """Manually trigger context generation for a project."""
    generator = ContextGenerator(db)
    
    check = await generator.check_project_ready(project_id)
    if not check["exists"]:
        raise HTTPException(404, "Project not found")
    
    result = await generator.generate_context(project_id, agent_id)
    db.commit()
    
    return {
        "status": "success",
        "context_md_length": len(result["context_md"]),
        "rules_count": result["rules_count"],
    }


@router.get("/{project_id}/context-status")
def get_context_status(
    project_id: str,
    db: Session = Depends(get_db),
):
    """Check if project context is ready."""
    generator = ContextGenerator(db)
    return generator.check_project_ready(project_id)
```

---

## Phase 5: Schemas

```python
# backend/app/schemas/project_rule.py

from pydantic import BaseModel, Field
from datetime import datetime


class ProjectRuleBase(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=500)
    globs: list[str] = Field(default_factory=list)
    content: str
    priority: int = 0


class ProjectRuleCreate(ProjectRuleBase):
    pass


class ProjectRuleUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    globs: list[str] | None = None
    content: str | None = None
    priority: int | None = None


class ProjectRuleRead(ProjectRuleBase):
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime | None

    class Config:
        from_attributes = True
```

---

## Phase 6: Testing

### 6.1 Unit Tests

```python
# backend/tests/test_context_generator.py

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.context_generator import (
    ContextGenerator,
    get_matching_rules,
)
from app.db.models import Project, ProjectRule


class TestGetMatchingRules:
    def test_no_files_returns_all_rules(self, db_session):
        # Setup
        project = Project(id="proj-1", name="Test")
        rule1 = ProjectRule(id="r1", project_id="proj-1", name="rule1", globs=["*.py"], content="...")
        rule2 = ProjectRule(id="r2", project_id="proj-1", name="rule2", globs=["*.ts"], content="...")
        db_session.add_all([project, rule1, rule2])
        db_session.flush()
        
        # Execute
        result = get_matching_rules(db_session, "proj-1", None)
        
        # Assert
        assert len(result) == 2

    def test_matches_glob_pattern(self, db_session):
        # Setup
        project = Project(id="proj-1", name="Test")
        rule_py = ProjectRule(id="r1", project_id="proj-1", name="python", globs=["**/*.py"], content="...")
        rule_ts = ProjectRule(id="r2", project_id="proj-1", name="typescript", globs=["**/*.ts"], content="...")
        db_session.add_all([project, rule_py, rule_ts])
        db_session.flush()
        
        # Execute
        result = get_matching_rules(db_session, "proj-1", ["backend/app/main.py"])
        
        # Assert
        assert len(result) == 1
        assert result[0].name == "python"

    def test_multiple_files_match_multiple_rules(self, db_session):
        # Setup
        project = Project(id="proj-1", name="Test")
        rule_py = ProjectRule(id="r1", project_id="proj-1", name="python", globs=["**/*.py"], content="...")
        rule_ts = ProjectRule(id="r2", project_id="proj-1", name="typescript", globs=["**/*.ts"], content="...")
        db_session.add_all([project, rule_py, rule_ts])
        db_session.flush()
        
        # Execute
        result = get_matching_rules(db_session, "proj-1", ["app/main.py", "frontend/app.ts"])
        
        # Assert
        assert len(result) == 2


class TestContextGenerator:
    @pytest.mark.anyio
    async def test_check_project_ready_missing_context(self, db_session):
        project = Project(id="proj-1", name="Test", context_md=None)
        db_session.add(project)
        db_session.flush()
        
        generator = ContextGenerator(db_session)
        result = await generator.check_project_ready("proj-1")
        
        assert result["ready"] is False
        assert result["has_context"] is False

    @pytest.mark.anyio
    async def test_check_project_ready_complete(self, db_session):
        project = Project(id="proj-1", name="Test", context_md="# Context")
        rule = ProjectRule(id="r1", project_id="proj-1", name="rule1", globs=[], content="...")
        db_session.add_all([project, rule])
        db_session.flush()
        
        generator = ContextGenerator(db_session)
        result = await generator.check_project_ready("proj-1")
        
        assert result["ready"] is True
```

### 6.2 Integration Tests

```python
# backend/tests/test_dispatch_with_context.py

import pytest
from app.services.task_orchestration import TaskOrchestrationService


class TestDispatchWithContext:
    @pytest.mark.anyio
    async def test_dispatch_without_context_triggers_gen(self, db_session):
        """Dispatch without context should trigger context generation."""
        # Setup project without context
        project = Project(id="proj-1", name="Test", repo_root="/tmp/test")
        task = Task(id="task-1", project="proj-1", title="Test task", status="todo")
        agent = Agent(id="agent-1", name="Test Agent", role="executor")
        db_session.add_all([project, task, agent])
        db_session.flush()
        
        # Execute
        service = TaskOrchestrationService(db_session)
        # Should raise or return context_gen action
        result = service.request_dispatch(
            task_id="task-1",
            agent_id="agent-1",
            actor="test",
            idempotency_key="test-key",
        )
        
        # Assert
        assert result.context["action"] == "context_gen_triggered"

    @pytest.mark.anyio
    async def test_dispatch_with_context_proceeds(self, db_session):
        """Dispatch with context should proceed normally."""
        # Setup project WITH context
        project = Project(id="proj-1", name="Test", repo_root="/tmp/test", context_md="# Context")
        rule = ProjectRule(id="r1", project_id="proj-1", name="rule1", globs=[], content="...")
        task = Task(id="task-1", project="proj-1", title="Test task", status="todo", acceptance_criteria=["AC1"])
        agent = Agent(id="agent-1", name="Test Agent", role="executor", model="claude-sonnet")
        db_session.add_all([project, rule, task, agent])
        db_session.flush()
        
        # Execute
        service = TaskOrchestrationService(db_session)
        result = service.request_dispatch(
            task_id="task-1",
            agent_id="agent-1",
            actor="test",
            idempotency_key="test-key",
        )
        
        # Assert - should be normal dispatch, not context_gen
        assert result.context.get("action") != "context_gen_triggered"


class TestSaveProjectContextMCP:
    @pytest.mark.anyio
    async def test_save_project_context_tool(self, db_session):
        """MCP tool save_project_context should save to DB."""
        # Setup project
        project = Project(id="proj-1", name="Test", repo_root="/tmp/test")
        db_session.add(project)
        db_session.flush()
        
        # Simulate MCP tool call
        router = CommandRouter(db_session, session_id="test")
        result = await router.execute_tool(
            "save_project_context",
            {
                "project_id": "proj-1",
                "context_md": "# Test Project\n## Stack\nPython + FastAPI",
                "rules": [
                    {"name": "api", "globs": ["app/api/**/*.py"], "content": "Use FastAPI routers"}
                ]
            }
        )
        
        # Assert
        assert result["status"] == "saved"
        assert result["rules_count"] == 1
        
        # Verify DB
        project = db_session.get(Project, "proj-1")
        assert "Test Project" in project.context_md
        assert project.context_generated is True
        
        rules = db_session.query(ProjectRule).filter_by(project_id="proj-1").all()
        assert len(rules) == 1
        assert rules[0].name == "api"
```

---

## Checklist triển khai

### Phase 1: Database (Day 1)
- [ ] Tạo migration `add_project_rules`
- [ ] Add `ProjectRule` model
- [ ] Add `context_generated` field to `Project`
- [ ] Run migration, verify schema

### Phase 2: MCP Tool + Service (Day 2-3)
- [ ] Add `save_project_context` tool to `TOOL_REGISTRY`
- [ ] Add handler `_handle_save_project_context` in `CommandRouter`
- [ ] Implement `ContextChecker` class
- [ ] Implement `get_matching_rules()` function
- [ ] Add unit tests

### Phase 3: Integrate Dispatch (Day 4-5)
- [ ] Update `command_builder.py` - inject context + rules vào prompt
- [ ] Update `task_orchestration.py` - add context check trước dispatch
- [ ] Add `_trigger_context_gen_dispatch()` method
- [ ] Add `CONTEXT_GEN_PROMPT` template
- [ ] Integration tests

### Phase 4: API Endpoints (Day 6)
- [ ] Add `/projects/{id}/rules` CRUD endpoints
- [ ] Add `/projects/{id}/context-status` endpoint
- [ ] Add schemas `ProjectRuleCreate`, `ProjectRuleRead`, etc.

### Phase 5: Testing & Polish (Day 7)
- [ ] End-to-end test: dispatch → context gen → save via MCP → retry original task
- [ ] Test với real project (clone mới)
- [ ] Verify token reduction
- [ ] Documentation

---

## Metrics để đo success

| Metric | Baseline | Target |
|--------|----------|--------|
| Avg iterations/task | 2.5 | 1.5 |
| Tokens/task | 250,000 | 150,000 |
| First-attempt success rate | 40% | 70% |

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM gen context sai | Quality giảm | Template constraints, validation trong MCP handler |
| Context quá dài | Token tăng | Hard limit 150 lines, reject trong handler |
| Agent không gọi MCP tool | Context không save | Prompt rõ ràng, acceptance criteria bắt buộc |
| MCP call fail | Context gen fail | Retry logic, error handling trong agent_runner |
| Glob matching chậm | Latency tăng | Index, cache rules per project |
| Stale context | Quality giảm | Manual regenerate via coordinator command |

## Cơ chế hoạt động MCP

```
agenticmatix dispatch context_gen task
    ↓
CLI spawn với --mcp-config (inject agenticmatix MCP server)
    ↓
Agent scan target repo
    ↓
Agent gọi tool: save_project_context(project_id, context_md, rules)
    ↓
MCP Server (agenticmatix) forward → POST /api/mcp/tools/call
    ↓
CommandRouter._handle_save_project_context() → Save DB
    ↓
Return success → Agent hoàn thành task
    ↓
Original task có thể dispatch (context ready)
```

**Target project không cần gì đặc biệt** - MCP server là của agenticmatix, inject khi dispatch.
