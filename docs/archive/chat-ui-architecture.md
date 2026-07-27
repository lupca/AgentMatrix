# Chat UI Architecture: Hierarchical Context + Multi-Session

> CTV2-055 Research Document | 2026-07-26

## Table of Contents

1. [Overview](#overview)
2. [Three-Level Chat Architecture](#three-level-chat-architecture)
3. [Session Lifecycle & Context Inheritance](#session-lifecycle--context-inheritance)
4. [DB Schema Changes](#db-schema-changes)
5. [Component Hierarchy](#component-hierarchy)
6. [Wireframes & Mockups](#wireframes--mockups)
7. [Token Caching Strategy](#token-caching-strategy)

---

## Overview

This document describes the architecture for extending the Control Tower chat system to support:

- **3-level hierarchical chat**: Global, Project, and Task contexts
- **Multi-session support**: Multiple conversation threads per context level
- **Session tabs UI**: Quick switching between active sessions
- **Token optimization**: Message ordering designed for maximum Anthropic prompt cache hits

### Current State Gaps

| Feature | Current | Target |
|---------|---------|--------|
| Chat levels | Task only | Global / Project / Task |
| Sessions per context | 1 implicit | Multiple with explicit management |
| Session switching | None | Tab-based UI |
| Context inheritance | CTV2-053 implemented | Leverage for caching |
| Global chat access | None | Floating button anywhere |

---

## Three-Level Chat Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          GLOBAL CONTEXT (Tier 1)                         │
│  ┌─────────────────────────────────────────────────────────────────────┐│
│  │ System prompt + gate rules + tool definitions                       ││
│  │ cache_control: ephemeral (5-min TTL)                                ││
│  └─────────────────────────────────────────────────────────────────────┘│
│                                  │                                       │
│                    ┌─────────────┴─────────────┐                        │
│                    ▼                           ▼                        │
│  ┌──────────────────────────┐   ┌──────────────────────────┐           │
│  │   PROJECT A CONTEXT      │   │   PROJECT B CONTEXT      │           │
│  │   (Tier 2)               │   │   (Tier 2)               │           │
│  │   ─────────────────────  │   │   ─────────────────────  │           │
│  │   Project description    │   │   Project description    │           │
│  │   context.md (≤25KB)     │   │   context.md (≤25KB)     │           │
│  │   Auto-memory (5 tasks)  │   │   Auto-memory (5 tasks)  │           │
│  │   cache_control: ephemeral│   │   cache_control: ephemeral│          │
│  │                          │   │                          │           │
│  │  ┌────────┐ ┌────────┐   │   │  ┌────────┐ ┌────────┐   │           │
│  │  │Task A1 │ │Task A2 │   │   │  │Task B1 │ │Task B2 │   │           │
│  │  │(Tier 3)│ │(Tier 3)│   │   │  │(Tier 3)│ │(Tier 3)│   │           │
│  │  └────────┘ └────────┘   │   │  └────────┘ └────────┘   │           │
│  └──────────────────────────┘   └──────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────────┘
```

### Context Level Definitions

| Level | Scope | Available Context | Use Cases |
|-------|-------|-------------------|-----------|
| **Global** | System-wide | System prompt, gate rules, tool schemas | General questions, cross-project queries, onboarding |
| **Project** | Single project | Global + project description + context.md + auto-memory | Project planning, architecture discussions, dependency analysis |
| **Task** | Single task | Global + project + task spec + plan + LangGraph state | Task execution, code review, debugging |

### Context Inheritance

Each level inherits from its parent:

```python
# Pseudo-code for context composition
def build_messages(session: Session) -> list[Message]:
    messages = []
    
    # Tier 1: Global (always present)
    messages.extend(global_context)
    messages[-1].cache_control = {"type": "ephemeral"}
    
    # Tier 2: Project (if project_id set)
    if session.project_id:
        messages.extend(project_context(session.project_id))
        messages[-1].cache_control = {"type": "ephemeral"}
    
    # Tier 3: Task (if task_id set)
    if session.task_id:
        messages.extend(task_context(session.task_id))
        # No cache_control on task tier (dynamic)
    
    # Tier 4: Conversation history (dynamic)
    messages.extend(session.messages)
    
    return messages
```

---

## Session Lifecycle & Context Inheritance

### Session States

```
┌─────────┐     create      ┌─────────┐    first msg    ┌─────────┐
│  NONE   │ ──────────────▶ │ CREATED │ ──────────────▶ │ ACTIVE  │
└─────────┘                 └─────────┘                 └─────────┘
                                                              │
                    ┌─────────────────────────────────────────┤
                    │                                         │
              30 days idle                              explicit close
                    │                                         │
                    ▼                                         ▼
             ┌──────────┐                              ┌──────────┐
             │ ARCHIVED │                              │  CLOSED  │
             └──────────┘                              └──────────┘
```

### Session Creation by Context Level

| Trigger | Context Level | Pre-selected Context |
|---------|---------------|----------------------|
| Click global chat button | Global | None |
| Open project detail page | Project | Current project |
| Open task detail page | Task | Current project + task |
| "New session" in existing panel | Same as current | Same as current |

### Context Pre-Selection Rules

```typescript
interface SessionContext {
  level: 'global' | 'project' | 'task';
  project_id?: string;
  task_id?: string;
}

function getContextFromRoute(route: string, params: RouteParams): SessionContext {
  if (route.startsWith('/tasks/')) {
    return {
      level: 'task',
      project_id: params.projectId || taskLookup(params.taskId).project,
      task_id: params.taskId
    };
  }
  if (route.startsWith('/projects/')) {
    return {
      level: 'project',
      project_id: params.projectId
    };
  }
  return { level: 'global' };
}
```

---

## DB Schema Changes

### Current Session Model

```python
class Session(Base):
    id = Column(String(36), primary_key=True)
    task_id = Column(String(20), ForeignKey("tasks.id"), nullable=True)
    thread_id = Column(String(100), nullable=True)
    messages = Column(JSON, default=list)
    selected_provider = Column(String(30), nullable=True)
    selected_model = Column(String(100), nullable=True)
    # ...
```

### Proposed Session Model

```python
from sqlalchemy import Enum
import enum

class ContextLevel(enum.Enum):
    GLOBAL = "global"
    PROJECT = "project"
    TASK = "task"

class SessionStatus(enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    CLOSED = "closed"

class Session(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    
    # NEW: Context hierarchy fields
    context_level = Column(
        Enum(ContextLevel), 
        nullable=False, 
        default=ContextLevel.GLOBAL,
        index=True
    )
    project_id = Column(
        String(50), 
        ForeignKey("projects.id", ondelete="SET NULL"), 
        nullable=True, 
        index=True
    )
    task_id = Column(
        String(20), 
        ForeignKey("tasks.id", ondelete="SET NULL"), 
        nullable=True, 
        index=True
    )
    
    # NEW: Session metadata
    title = Column(String(200), nullable=True)  # Auto-generated or user-set
    status = Column(
        Enum(SessionStatus), 
        nullable=False, 
        default=SessionStatus.ACTIVE,
        index=True
    )
    pinned = Column(Boolean, default=False)  # Keep at top of session list
    
    # Existing fields
    thread_id = Column(String(100), nullable=True, index=True)
    current_gate = Column(String(20), nullable=True)
    checkpoint_id = Column(String(100), nullable=True, index=True)
    state_payload = Column(JSON, nullable=True)
    selected_provider = Column(String(30), nullable=True)
    selected_model = Column(String(100), nullable=True)
    messages = Column(JSON, default=list)
    
    # NEW: Usage tracking for session-level analytics
    message_count = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_activity_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    project = relationship("Project", backref="sessions")
    task = relationship("Task", back_populates="sessions")
    llm_usages = relationship("LLMUsage", back_populates="session")

    __table_args__ = (
        # Ensure task_id implies project_id is set
        CheckConstraint(
            "(task_id IS NULL) OR (project_id IS NOT NULL)",
            name="ck_sessions_task_requires_project"
        ),
        # Ensure context_level matches the FK state
        CheckConstraint(
            "(context_level = 'global' AND project_id IS NULL AND task_id IS NULL) OR "
            "(context_level = 'project' AND project_id IS NOT NULL AND task_id IS NULL) OR "
            "(context_level = 'task' AND project_id IS NOT NULL AND task_id IS NOT NULL)",
            name="ck_sessions_context_level_consistency"
        ),
        # Composite index for efficient session listing queries
        Index('ix_sessions_context_listing', 
              'context_level', 'project_id', 'status', 'last_activity_at'),
    )
```

### Migration Script

```python
# alembic/versions/011_session_hierarchical_context.py

def upgrade():
    # Add new columns
    op.add_column('sessions', sa.Column(
        'context_level', 
        sa.Enum('global', 'project', 'task', name='contextlevel'),
        nullable=True
    ))
    op.add_column('sessions', sa.Column(
        'project_id', 
        sa.String(50), 
        sa.ForeignKey('projects.id', ondelete='SET NULL'),
        nullable=True
    ))
    op.add_column('sessions', sa.Column('title', sa.String(200), nullable=True))
    op.add_column('sessions', sa.Column(
        'status',
        sa.Enum('active', 'archived', 'closed', name='sessionstatus'),
        nullable=True,
        server_default='active'
    ))
    op.add_column('sessions', sa.Column('pinned', sa.Boolean(), server_default='false'))
    op.add_column('sessions', sa.Column('message_count', sa.Integer(), server_default='0'))
    op.add_column('sessions', sa.Column('total_tokens', sa.Integer(), server_default='0'))
    op.add_column('sessions', sa.Column(
        'last_activity_at', 
        sa.DateTime(timezone=True), 
        server_default=sa.func.now()
    ))
    
    # Backfill existing sessions
    # - Sessions with task_id -> context_level='task', derive project_id from task
    # - Sessions without task_id -> context_level='global'
    op.execute("""
        UPDATE sessions s
        SET 
            context_level = CASE 
                WHEN task_id IS NOT NULL THEN 'task' 
                ELSE 'global' 
            END,
            project_id = (
                SELECT t.project 
                FROM tasks t 
                WHERE t.id = s.task_id
            ),
            status = 'active',
            message_count = COALESCE(json_array_length(messages::json), 0),
            last_activity_at = updated_at
    """)
    
    # Make context_level NOT NULL after backfill
    op.alter_column('sessions', 'context_level', nullable=False)
    op.alter_column('sessions', 'status', nullable=False)
    
    # Create indexes
    op.create_index(
        'ix_sessions_project_id', 
        'sessions', 
        ['project_id']
    )
    op.create_index(
        'ix_sessions_context_listing',
        'sessions',
        ['context_level', 'project_id', 'status', 'last_activity_at']
    )
    
    # Add check constraints
    op.create_check_constraint(
        'ck_sessions_task_requires_project',
        'sessions',
        '(task_id IS NULL) OR (project_id IS NOT NULL)'
    )
    op.create_check_constraint(
        'ck_sessions_context_level_consistency',
        'sessions',
        "(context_level = 'global' AND project_id IS NULL AND task_id IS NULL) OR "
        "(context_level = 'project' AND project_id IS NOT NULL AND task_id IS NULL) OR "
        "(context_level = 'task' AND project_id IS NOT NULL AND task_id IS NOT NULL)"
    )

def downgrade():
    op.drop_constraint('ck_sessions_context_level_consistency', 'sessions')
    op.drop_constraint('ck_sessions_task_requires_project', 'sessions')
    op.drop_index('ix_sessions_context_listing', 'sessions')
    op.drop_index('ix_sessions_project_id', 'sessions')
    op.drop_column('sessions', 'last_activity_at')
    op.drop_column('sessions', 'total_tokens')
    op.drop_column('sessions', 'message_count')
    op.drop_column('sessions', 'pinned')
    op.drop_column('sessions', 'status')
    op.drop_column('sessions', 'title')
    op.drop_column('sessions', 'project_id')
    op.drop_column('sessions', 'context_level')
```

### Query Patterns

```python
# Get all active sessions for a project (including task-level)
sessions = db.query(Session).filter(
    Session.project_id == project_id,
    Session.status == SessionStatus.ACTIVE
).order_by(
    Session.pinned.desc(),
    Session.last_activity_at.desc()
).all()

# Get global sessions only
global_sessions = db.query(Session).filter(
    Session.context_level == ContextLevel.GLOBAL,
    Session.status == SessionStatus.ACTIVE
).order_by(Session.last_activity_at.desc()).limit(10).all()

# Get sessions for a specific task
task_sessions = db.query(Session).filter(
    Session.task_id == task_id,
    Session.status == SessionStatus.ACTIVE
).order_by(Session.created_at.desc()).all()
```

---

## Component Hierarchy

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              App.tsx                                     │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                        ChatProvider (Context)                      │  │
│  │  - activeSession: Session | null                                   │  │
│  │  - sessions: Map<string, Session[]> (by context)                   │  │
│  │  - openSession(context): void                                      │  │
│  │  - switchSession(sessionId): void                                  │  │
│  │  - createSession(context): Promise<Session>                        │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                   │                                      │
│         ┌─────────────────────────┴─────────────────────────┐           │
│         ▼                                                   ▼           │
│  ┌─────────────────┐                              ┌─────────────────┐   │
│  │ GlobalChatButton│                              │    Layout       │   │
│  │ (fixed bottom-  │                              │                 │   │
│  │  right corner)  │                              │  ┌───────────┐  │   │
│  │                 │                              │  │ Pages     │  │   │
│  │  onClick:       │                              │  │           │  │   │
│  │  openGlobalChat │                              │  │ProjectPage│  │   │
│  └─────────────────┘                              │  │  TaskPage │  │   │
│                                                   │  │  etc.     │  │   │
│                                                   │  └───────────┘  │   │
│                                                   └─────────────────┘   │
│                                   │                                      │
│                                   ▼                                      │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │                         ChatSidebar                                │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                    SessionTabs                               │  │  │
│  │  │  [Session 1] [Session 2] [+ New]                            │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                    ContextSelector                           │  │  │
│  │  │  Level: [Global ▼]  Project: [control-tower-v2 ▼]           │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────────────────┐  │  │
│  │  │                      ChatPanel                               │  │  │
│  │  │  (existing component, unchanged)                            │  │  │
│  │  └─────────────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

### New Components

| Component | Responsibility | Props |
|-----------|---------------|-------|
| `ChatProvider` | React context for global chat state | `children` |
| `GlobalChatButton` | Fixed floating button (bottom-right) | `onClick` |
| `ChatSidebar` | Collapsible sidebar container | `isOpen`, `onClose`, `context` |
| `SessionTabs` | Tab bar for session switching | `sessions`, `activeId`, `onSwitch`, `onCreate` |
| `ContextSelector` | Dropdown for context level/project | `context`, `onChange` |
| `SessionList` | List of sessions for current context | `sessions`, `onSelect` |

### Integration Points

```tsx
// ChatProvider wraps the app
function App() {
  return (
    <ChatProvider>
      <Router>
        <Layout>
          <Routes>...</Routes>
        </Layout>
        <GlobalChatButton />
        <ChatSidebar />
      </Router>
    </ChatProvider>
  );
}

// ProjectDetailPage auto-opens project context
function ProjectDetailPage() {
  const { openSession } = useChat();
  const { id } = useParams();
  
  useEffect(() => {
    openSession({ level: 'project', project_id: id });
  }, [id]);
  
  return (
    <div className="grid grid-cols-12">
      <div className="col-span-8">{/* Project content */}</div>
      <div className="col-span-4">
        <ChatPanelManager context={{ level: 'project', project_id: id }} />
      </div>
    </div>
  );
}
```

---

## Wireframes & Mockups

### Global Chat (Floating Button + Sidebar)

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Control Tower                                              [User Menu] ▼  │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│    ┌──────────────────────────────────────────────────────────────────┐   │
│    │                                                                   │   │
│    │                                                                   │   │
│    │                    Main Page Content                              │   │
│    │                    (Dashboard / Projects / Tasks / etc.)          │   │
│    │                                                                   │   │
│    │                                                                   │   │
│    │                                                                   │   │
│    │                                                                   │   │
│    │                                                                   │   │
│    │                                                                   │   │
│    └──────────────────────────────────────────────────────────────────┘   │
│                                                                            │
│                                                                            │
│                                                         ┌──────────────┐  │
│                                                         │ 💬 AI Chat   │  │
│                                                         │     •        │  │
│                                                         └──────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
                                                          ▲
                                                          │ Floating button
                                                          │ (fixed position)
```

### Global Chat Sidebar (Expanded)

```
┌──────────────────────────────────────────────────┬─────────────────────────┐
│  Control Tower                        [User] ▼   │        AI Assistant     │
├──────────────────────────────────────────────────┤   ╔═══════════════════╗ │
│                                                  │   ║ Global │ + New    ║ │
│                                                  │   ╠═══════════════════╣ │
│                                                  │   ║ Context: 🌐 Global║ │
│    Main Page Content                             │   ╠═══════════════════╣ │
│    (slightly narrower when sidebar open)         │   ║                   ║ │
│                                                  │   ║  🤖 Hi! I'm your  ║ │
│                                                  │   ║  Control Tower    ║ │
│                                                  │   ║  assistant. How   ║ │
│                                                  │   ║  can I help?      ║ │
│                                                  │   ║                   ║ │
│                                                  │   ║ ───────────────── ║ │
│                                                  │   ║                   ║ │
│                                                  │   ║ 👤 What projects  ║ │
│                                                  │   ║    are active?    ║ │
│                                                  │   ║                   ║ │
│                                                  │   ║ ───────────────── ║ │
│                                                  │   ║                   ║ │
│                                                  │   ╠═══════════════════╣ │
│                                                  │   ║ [Type message...] ║ │
│                                                  │   ║ [Model ▼] [Send]  ║ │
│                                                  │   ╚═══════════════════╝ │
└──────────────────────────────────────────────────┴─────────────────────────┘
```

### Project Detail Page with Chat Panel

```
┌────────────────────────────────────────────────────────────────────────────┐
│  ← Back to Projects                                         [User Menu] ▼  │
├────────────────────────────────────────────────────────────────────────────┤
│                                                                            │
│  ┌─ control-tower-v2 ─────────────────────────┐  ┌─ Project Chat ────────┐│
│  │ 📁 Control Tower V2 - LangGraph Redesign   │  │ [Proj] [Task:CTV-055]││
│  │ Status: active     Tasks: 12/55 done       │  │ ─────────────────────││
│  │                                            │  │ Context: 📁 ctv2     ││
│  │ ───────────────────────────────────────────│  │ ─────────────────────││
│  │ Project description here...                │  │                      ││
│  │                                            │  │ 🤖 I see 55 tasks    ││
│  │ ══════════════════════════════════════════ │  │    in this project.  ││
│  │                                            │  │    12 are complete.  ││
│  │ Task Execution Queue                       │  │                      ││
│  │ ┌────────────────────────────────────────┐ │  │ 👤 What's blocking   ││
│  │ │ ID    │ Title          │ Status │ Gate │ │  │    CTV2-054?         ││
│  │ ├───────┼────────────────┼────────┼──────┤ │  │                      ││
│  │ │CTV-055│ Research: Chat │dispatch│ spec │ │  │ 🤖 CTV2-054 depends  ││
│  │ │CTV-054│ Implement tabs │ todo   │ plan │ │  │    on CTV2-053...    ││
│  │ │CTV-053│ Context hier.. │ done   │ done │ │  │                      ││
│  │ └────────────────────────────────────────┘ │  │ ─────────────────────││
│  │                                            │  │ [Message...] [Send]  ││
│  └────────────────────────────────────────────┘  └──────────────────────┘│
└────────────────────────────────────────────────────────────────────────────┘
```

### Task Detail Page with Session Tabs

```
┌────────────────────────────────────────────────────────────────────────────┐
│  ← Back to Tasks                CTV2-055                    [User Menu] ▼  │
├────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────┬───────────────────────────┐
│  │ CTV2-055: Research: Chat UI                 │ Task Copilot              │
│  │ Status: dispatched  Executor: @claude-opus  │ ┌─────────────────────────┤
│  │ ───────────────────────────────────────────-│ │[Main][Debug][+]   ✕    │
│  │                                             │ ├─────────────────────────┤
│  │ Acceptance Criteria                         │ │Context: 📋 CTV2-055    │
│  │ ☐ Architecture Document                     │ │Project: control-tower   │
│  │ ☐ DB Schema Proposal                        │ ├─────────────────────────┤
│  │ ☐ Wireframes/Mockups                        │ │                         │
│  │ ☐ Token Caching Strategy                    │ │🤖 Working on the        │
│  │                                             │ │  architecture doc...    │
│  │ ═══════════════════════════════════════════ │ │                         │
│  │                                             │ │👤 Add a section about   │
│  │ Plan                                        │ │  session archiving      │
│  │ 1. Read current models.py                   │ │                         │
│  │ 2. Read context_hierarchy.py                │ │🤖 Added. I've included  │
│  │ 3. Design DB schema changes                 │ │  a 30-day idle policy.  │
│  │ 4. Create wireframes                        │ │                         │
│  │ 5. Document token caching                   │ ├─────────────────────────┤
│  │                                             │ │[Type message...]        │
│  │                                             │ │[sonnet-4 ▼]    [Send]   │
│  └─────────────────────────────────────────────┴─────────────────────────── │
└────────────────────────────────────────────────────────────────────────────┘

Session Tab States:
┌────────────────────────────────────────────────────────────┐
│ [Main ●] [Debug] [Research] [+ New]                    ✕  │
│  ▲       ▲       ▲           ▲                             │
│  │       │       │           └── Create new session        │
│  │       │       └── User-named session                    │
│  │       └── Inactive tab                                  │
│  └── Active tab with unread indicator                      │
└────────────────────────────────────────────────────────────┘
```

### Session List Dropdown

```
┌─────────────────────────────────────┐
│ Sessions for control-tower-v2      │
├─────────────────────────────────────┤
│ 📌 Architecture Planning     2h ago│
│    "Discussing the new chat..."    │
│ ─────────────────────────────────  │
│ 📋 CTV2-055: Research        10m   │
│    "Working on wireframes..."      │
│ ─────────────────────────────────  │
│ 📋 CTV2-053: Context Hier   1d ago │
│    "Implemented cache_control"     │
│ ─────────────────────────────────  │
│ [+ New Project Session]            │
├─────────────────────────────────────┤
│ 🗄️ View Archived Sessions (12)     │
└─────────────────────────────────────┘
```

---

## Token Caching Strategy

### Anthropic Prompt Caching Overview

Anthropic's prompt caching uses `cache_control` markers to identify cacheable prefixes:

| Property | Value |
|----------|-------|
| Cache TTL | 5 minutes |
| Minimum cacheable | 1024 tokens (Sonnet), 2048 tokens (Opus) |
| Price discount | 90% for cache hits |
| Write cost | 25% premium for initial cache write |

### Message Ordering for Cache Optimization

The key insight: **cache hits require exact prefix matches**. Our 3-tier context hierarchy is designed for this:

```
┌────────────────────────────────────────────────────────────────────┐
│                         REQUEST STRUCTURE                           │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ TIER 1: Global Context (System)                              │ │
│  │ ─────────────────────────────────────────────────────────────│ │
│  │ "You are Control Tower V2..."                                │ │
│  │ Gate rules, tool definitions                                 │ │
│  │ ≈ 3,000 tokens (stable across ALL requests)                  │ │
│  │                                                              │ │
│  │ cache_control: { type: "ephemeral" }  ← CACHE BREAKPOINT 1  │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│                              ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ TIER 2: Project Context (User message)                       │ │
│  │ ─────────────────────────────────────────────────────────────│ │
│  │ [Project Context: control-tower-v2]                          │ │
│  │ Description + context.md + auto-memory                       │ │
│  │ ≈ 5,000-20,000 tokens (capped at 25KB)                       │ │
│  │                                                              │ │
│  │ cache_control: { type: "ephemeral" }  ← CACHE BREAKPOINT 2  │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│                              ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ TIER 3: Task Context (System message)                        │ │
│  │ ─────────────────────────────────────────────────────────────│ │
│  │ Task [CTV2-055]: title, plan, LangGraph state                │ │
│  │ ≈ 500-2,000 tokens                                           │ │
│  │                                                              │ │
│  │ NO cache_control (changes too frequently)                    │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                              │                                     │
│                              ▼                                     │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ TIER 4: Conversation History (User/Assistant turns)          │ │
│  │ ─────────────────────────────────────────────────────────────│ │
│  │ Previous messages in this session                            │ │
│  │ ≈ Variable (grows with conversation)                         │ │
│  │                                                              │ │
│  │ NO cache_control (changes every turn)                        │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### Cache Hit Scenarios

| Scenario | Cache Behavior | Tokens Saved |
|----------|----------------|--------------|
| Same user, same task, next message | Tier 1 + 2 hit, Tier 3 partial | ~95% of context |
| Same user, different task, same project | Tier 1 + 2 hit | ~80% of context |
| Same user, different project | Tier 1 hit only | ~15% of context |
| Different user, same project (within 5 min) | Tier 1 + 2 hit | ~80% of context |
| New session, no cache | No hits (cold start) | 0% |

### Estimated Token Savings

**Assumptions:**
- Global context: 3,000 tokens
- Average project context: 10,000 tokens
- Average task context: 1,000 tokens
- Average conversation: 5 messages, 500 tokens each
- Cache hit rate: 70% (same project), 30% (different project)

**Per-Request Cost (Sonnet, $3/1M input)**

| Scenario | Without Cache | With Cache | Savings |
|----------|---------------|------------|---------|
| Cold start | 16,500 × $3/1M = $0.0495 | Same + 25% write | -$0.0124 |
| Warm, same project | 16,500 × $3/1M = $0.0495 | 3,500 × $3 + 13,000 × $0.30 = $0.0144 | **71% cheaper** |
| Warm, different project | 16,500 × $3/1M = $0.0495 | 13,500 × $3 + 3,000 × $0.30 = $0.0414 | 16% cheaper |

**Monthly Projection (10,000 messages)**

| Without Cache | With Cache (70% hit rate) | Monthly Savings |
|---------------|---------------------------|-----------------|
| $495 | ~$180 | **~$315/month (64%)** |

### Multi-Session Cache Sharing

The hierarchical design enables cache sharing across sessions:

```
Session A (Task CTV2-055)           Session B (Task CTV2-056)
         │                                    │
         ▼                                    ▼
┌─────────────────────────────────────────────────────────────┐
│              SHARED CACHE PREFIX                             │
│  Tier 1: Global context (3,000 tokens)                      │
│  Tier 2: Project context for control-tower-v2 (10,000 tok)  │
│                                                             │
│  Total cached: 13,000 tokens                                │
│  Cache cost: $0.039 (one-time write premium)                │
│  Cache benefit: $0.0351/request × many requests             │
└─────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
┌──────────────────┐              ┌──────────────────┐
│ Task A context   │              │ Task B context   │
│ (1,000 tokens)   │              │ (1,200 tokens)   │
│ Conversation A   │              │ Conversation B   │
│ (2,500 tokens)   │              │ (1,800 tokens)   │
└──────────────────┘              └──────────────────┘
```

### Implementation in `context_hierarchy.py`

The current implementation already follows this pattern:

```python
def build_messages(self, session, project_id=None):
    messages = []
    
    # Tier 1: Global (with cache_control)
    global_ctx = self.get_global_context()
    if global_ctx:
        messages.extend(global_ctx)
        messages[-1]["cache_control"] = {"type": "ephemeral"}  # ← Cache breakpoint
    
    # Tier 2: Project (with cache_control)
    project_ctx = self.get_project_context(project_id)
    if project_ctx:
        messages.extend(project_ctx)
        messages[-1]["cache_control"] = {"type": "ephemeral"}  # ← Cache breakpoint
    
    # Tier 3: Task (no cache_control - dynamic)
    messages.extend(self.get_task_context(session))
    
    return messages
```

### Optimization Recommendations

1. **Keep global context stable**: Avoid frequent changes to `global_context.md`
2. **Cap project context at 25KB**: Already implemented via `PROJECT_CONTEXT_MAX_CHARS`
3. **Use session compaction**: Compact old messages to reduce Tier 4 size (implemented via `compact_context`)
4. **Prefer project-level sessions**: When possible, use project context instead of task context to maximize cache sharing
5. **Batch operations within 5 minutes**: Cache TTL is 5 minutes - group related queries

### Monitoring Cache Performance

Add to `LLMUsage` tracking:

```python
class LLMUsage(Base):
    # Existing fields...
    cached_tokens = Column(Integer, nullable=False, default=0)  # Already exists!
    cache_creation_tokens = Column(Integer, nullable=False, default=0)  # NEW
    cache_read_tokens = Column(Integer, nullable=False, default=0)  # NEW
```

Dashboard query for cache hit rate:

```sql
SELECT 
    DATE(created_at) as date,
    SUM(cache_read_tokens) as cached,
    SUM(input_tokens) as total,
    ROUND(100.0 * SUM(cache_read_tokens) / NULLIF(SUM(input_tokens), 0), 1) as hit_rate_pct
FROM llm_usage
WHERE created_at > NOW() - INTERVAL '7 days'
GROUP BY DATE(created_at)
ORDER BY date DESC;
```

---

## Summary

This architecture enables:

1. **Flexible chat access**: Global, project, or task-level conversations
2. **Session management**: Multiple sessions per context with tabs UI
3. **Context inheritance**: Automatic pre-selection based on current page
4. **Token efficiency**: 60-70% cost reduction through hierarchical caching
5. **Smooth migration**: Existing sessions backfilled to new schema

### Implementation Priority

1. **Phase 1**: DB schema migration (Session model changes)
2. **Phase 2**: Backend API for session CRUD by context level
3. **Phase 3**: ChatProvider context + GlobalChatButton
4. **Phase 4**: SessionTabs + ContextSelector components
5. **Phase 5**: Integration into ProjectDetailPage
6. **Phase 6**: Cache monitoring dashboard
