# CTV2-059: Research - Chat Data Architecture for User Data Manipulation

> Task: [[CTV2-059]]  
> Date: 2026-07-27  
> Status: Complete

## Executive Summary

This research analyzes architecture patterns for enabling users to query and manipulate data (projects, tasks) via chat in an agentic OS. The recommended approach is **Option C: Hybrid (Structured State Snapshot + Tools)** which balances quality output with token efficiency by injecting a compact state summary into the system prompt for reads while using tools for mutations.

---

## AC1: LangGraph State Management, Memory & Persistence Patterns

### Core Concepts

**1. Checkpointing & Persistence**
LangGraph persistence saves graph state after every node execution (not just at the end), enabling:
- Resume after interruption, timeout, or service restart
- Human-in-the-loop approval flows
- Long-running agent conversations

**Persistence backends:**
| Backend | Use Case |
|---------|----------|
| `MemorySaver` | Development only, resets on restart |
| `SqliteSaver` | Local testing with persistence |
| `PostgresSaver` | Production |

```python
from langgraph.checkpoint.postgres import PostgresSaver

graph = builder.compile(checkpointer=PostgresSaver(...))
result = graph.invoke(input, config={"configurable": {"thread_id": "session-123"}})
```

**2. Thread Management**
- Same `thread_id` = same conversation state
- Different `thread_id` = isolated, independent conversations
- Thread isolation prevents cross-session data leakage

**3. Memory Patterns**
- **Short-term memory**: Current conversation context (in-graph state)
- **Long-term memory**: Persisted checkpoints across sessions
- **Working memory**: Rolling window of recent messages + summarized history

**Key Pattern: Rolling Context Window**
```
[System Prompt] + [Summary of turns 1-N] + [Last K turns verbatim]
```
This reduces token usage while maintaining context awareness.

### Relevance to Control Tower

Current implementation already uses:
- PostgreSQL as persistence layer (✓)
- Session model with `checkpoint_id`, `state_payload` columns (✓)
- Thread isolation via `session.id` (✓)

Missing: Systematic approach to inject data context into chat.

---

## AC2: Agentic OS Architecture Survey

### LangGraph Patterns

**Tool-First Architecture**
- Define tools for each operation (list_projects, create_task)
- LLM decides when to call tools based on user intent
- Tool results are appended to conversation for reasoning

**Pros**: Explicit control, auditable, supports deferred tool loading  
**Cons**: Each tool call adds latency and token overhead

**Current CTV2 Implementation:**
```python
EAGER_TOOLS = [
    {"name": "pm_create_task", ...},
    {"name": "get_status", ...}
]
DEFERRED_TOOLS = [...]  # Loaded on-demand via tool search
```

### AutoGen Multi-Agent Patterns

**Shared Blackboard Pattern**
- Agents post/read from shared message bus
- Group chat with turn management
- Manager agents coordinate specialized workers

**Communication Models:**
1. **Peer topology**: O(n²) message overhead
2. **Hierarchical**: Manager delegates, O(n) overhead
3. **Event-driven**: Agents react to triggers

**Relevance**: AutoGen is in maintenance mode; patterns inform but don't dictate.

### CrewAI Task Delegation

**Three State Layers:**
1. **Workflow state**: Durable facts for resume (projects, tasks)
2. **Agent working memory**: Short-lived reasoning context
3. **Event log**: Append-only audit trail

**Key Insight**: State split prevents bloat—agents see what they need, not everything.

**Capability Matching**: Tasks routed to agents based on declared skills (already implemented in CTV2's `Agent.capabilities`).

### Common Anti-Patterns

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| Full context reload | Token explosion | Rolling window + summary |
| Unbounded RAG retrieval | Stale data, high latency | Just-in-time tool calls |
| No state separation | Cross-agent confusion | Layered state model |
| Synchronous tool chains | Latency accumulation | Parallel where independent |

---

## AC3: Architecture Options

### Option A: Pure Tool-Based Approach

```
User: "What projects do I have?"
    ↓
LLM: calls list_projects()
    ↓
Tool: returns [{id: "ctv2", name: "Control Tower V2"}, ...]
    ↓
LLM: "You have 3 projects: Control Tower V2, ..."
```

**Implementation:**
```python
TOOLS = [
    {
        "name": "list_projects",
        "description": "List all projects with id, name, status",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "list_tasks",
        "description": "List tasks, optionally filtered by project or status",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "status": {"type": "string", "enum": ["todo", "dispatched", "done"]}
            }
        }
    },
    {
        "name": "create_project",
        "description": "Create a new project",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"}
            },
            "required": ["id", "name"]
        }
    }
]
```

**Token Analysis (per query):**
- System prompt: ~500 tokens
- Tool schemas (4 tools): ~400 tokens
- Tool call overhead: ~100 tokens
- Tool result: ~50-500 tokens (depending on data size)
- **Total per turn**: ~1,000-1,500 tokens

**Pros:**
- Explicit, auditable operations
- Fresh data on every call
- Supports complex filters
- Deferred loading reduces idle context

**Cons:**
- Every read = tool call = tokens + latency
- Repeated queries don't benefit from caching
- LLM must "decide" to call tool (can fail)

### Option B: RAG-Based Approach

```
User: "What projects do I have?"
    ↓
Embed query → Vector search → Retrieve relevant chunks
    ↓
LLM: reasons over retrieved context
    ↓
Response with data
```

**Implementation:**
```python
# Index projects/tasks into vector store
embeddings = embed(f"Project: {p.name}. Description: {p.description}")
vector_store.upsert(id=p.id, embedding=embeddings, metadata={...})

# Query
results = vector_store.query(embed(user_query), top_k=10)
context = format_results(results)
# Inject into prompt
```

**Token Analysis:**
- System prompt: ~500 tokens
- Retrieved context: ~1,000-2,000 tokens
- No tool overhead
- **Total per turn**: ~1,500-2,500 tokens

**Pros:**
- Natural language queries ("projects related to authentication")
- No tool-call decision needed
- Semantic matching

**Cons:**
- Indexing overhead (must re-index on every change)
- Stale data risk (cache invalidation problem)
- Overkill for structured data (projects/tasks have schemas)
- Higher token cost for simple queries

### Option C: Hybrid (Structured State Snapshot + Tools) — RECOMMENDED

```
System Prompt:
  "You are the Control Tower assistant.
   
   ## Current Context
   Projects (3):
   - ctv2: Control Tower V2 (active, 12 tasks)
   - auth: Auth Service (active, 5 tasks)
   - infra: Infrastructure (archived, 0 tasks)
   
   Recent tasks in ctv2:
   - CTV2-059: Research chat architecture (dispatched)
   - CTV2-058: Add session management (done)
   
   Use tools for: creating/updating data, detailed queries"
```

**Implementation:**
```python
def build_context_snapshot(session: Session) -> str:
    """Generate compact state summary for system prompt."""
    projects = db.query(Project).filter(Project.status == "active").all()
    
    lines = ["## Current Context", f"Projects ({len(projects)}):"]
    for p in projects:
        task_count = db.query(Task).filter(Task.project == p.id).count()
        lines.append(f"- {p.id}: {p.name} ({p.status}, {task_count} tasks)")
    
    if session.project_id:
        recent_tasks = (
            db.query(Task)
            .filter(Task.project == session.project_id)
            .order_by(Task.updated_at.desc())
            .limit(5)
            .all()
        )
        lines.append(f"\nRecent tasks in {session.project_id}:")
        for t in recent_tasks:
            lines.append(f"- {t.id}: {t.title[:40]} ({t.status})")
    
    return "\n".join(lines)

# Inject into system prompt
system_prompt = BASE_SYSTEM_PROMPT + "\n\n" + build_context_snapshot(session)
```

**Token Analysis:**
- System prompt + snapshot: ~600-800 tokens (fixed overhead, cacheable)
- Tool schemas (mutations only): ~200 tokens
- No read tool calls for basic queries
- **Total per turn**: ~800-1,000 tokens (reads), ~1,200 tokens (writes)

**Token Savings with Prompt Caching:**
```
Without caching: 50 turns × 1,000 tokens = 50,000 tokens
With caching:    1 × 800 + 49 × 200 = 10,600 tokens (79% reduction)
```

**Pros:**
- Zero-token reads for "what projects exist?"
- Prompt caching yields 80%+ savings on repeated turns
- Tools only for mutations (controlled, auditable)
- Always-current data (snapshot rebuilt per session)
- Context-aware from turn 1 (no cold-start problem)

**Cons:**
- Snapshot can grow with data scale (mitigate: pagination, scope)
- Must rebuild snapshot on data changes within session
- Not suitable for complex analytical queries

---

## AC4: Trade-off Analysis

### Quantitative Comparison

| Metric | Option A (Tools) | Option B (RAG) | Option C (Hybrid) |
|--------|-----------------|----------------|-------------------|
| **Tokens/read query** | 1,000-1,500 | 1,500-2,500 | 100-300 (cached) |
| **Tokens/write query** | 1,200-1,800 | 1,800-2,800 | 1,000-1,500 |
| **50-turn session** | ~60,000 | ~100,000 | ~15,000 |
| **Latency/read** | 500-1,000ms | 800-1,500ms | 200-400ms |
| **Latency/write** | 500-1,000ms | 800-1,500ms | 500-1,000ms |
| **Data freshness** | Real-time | May be stale | Real-time |
| **Implementation complexity** | Low | High | Medium |

### Qualitative Comparison

| Factor | Option A | Option B | Option C |
|--------|----------|----------|----------|
| **Quality for simple queries** | Good | Overkill | Excellent |
| **Quality for complex queries** | Excellent | Good | Good (fallback to tools) |
| **Cold-start awareness** | Poor (must call tool) | Good | Excellent |
| **Auditability** | Excellent | Poor | Good |
| **Scalability (100+ projects)** | Excellent | Good | Needs pagination |
| **Maintenance burden** | Low | High (indexing) | Low |

### Token Cost Breakdown (50-turn session)

```
Option A: Pure Tools
├── System prompt (50×): 500 × 50 = 25,000
├── Tool schemas (50×): 400 × 50 = 20,000
├── Tool calls (30 reads): 150 × 30 = 4,500
├── Tool results (30×): 200 × 30 = 6,000
└── User/Assistant turns: ~5,000
Total: ~60,500 tokens

Option C: Hybrid (with caching)
├── System prompt + snapshot (cached after 1st): 800 + (49 × 80*) = 4,720
├── Tool schemas (mutations, 20×): 200 × 20 = 4,000
├── Tool calls (5 writes): 150 × 5 = 750
├── Tool results (5×): 200 × 5 = 1,000
└── User/Assistant turns: ~5,000
Total: ~15,470 tokens

* 90% cache hit rate × 800 tokens = ~80 tokens/turn
```

**Savings: 74% reduction with Option C**

---

## AC5: Recommended Architecture

### Recommendation: Option C (Hybrid)

**Primary Justification:**

1. **Aligns with project goals**:
   - Quality output: AI knows context from turn 1
   - Token efficiency: 74% reduction vs pure tools

2. **Leverages existing infrastructure**:
   - PostgreSQL already stores projects/tasks
   - Session model has `project_id` for scoping
   - Tool definitions already exist for mutations

3. **Practical trade-offs**:
   - Simple queries (most common) are near-zero-cost
   - Complex queries fall back to tools
   - No new infrastructure (no vector store needed)

### Implementation Outline

```
┌─────────────────────────────────────────────────────────┐
│                    Chat Request                         │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│              build_context_snapshot()                    │
│  • Query active projects (summary)                       │
│  • Query recent tasks for session's project              │
│  • Format as compact markdown                            │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│              System Prompt Injection                     │
│  BASE_PROMPT + "## Current Context\n" + snapshot         │
│  + mutation tools (create_*, update_*, delete_*)         │
└───────────────────────┬─────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────┐
│                    LLM Call                              │
│  • Cache hit on stable system prompt prefix              │
│  • User query appended                                   │
│  • If mutation needed → tool call → execute → respond    │
│  • If read query → answer directly from context          │
└─────────────────────────────────────────────────────────┘
```

### Key Implementation Details

**1. Snapshot Scoping by Context Level**

```python
def build_context_snapshot(session: Session, db: Session) -> str:
    if session.context_level == "global":
        # All active projects, no task details
        return build_global_snapshot(db)
    elif session.context_level == "project":
        # This project + its recent tasks
        return build_project_snapshot(session.project_id, db)
    elif session.context_level == "task":
        # Project + this task's full details
        return build_task_snapshot(session.project_id, session.task_id, db)
```

**2. Snapshot Size Limits**

```python
MAX_PROJECTS_IN_SNAPSHOT = 10  # Summarize rest as "+N more"
MAX_TASKS_IN_SNAPSHOT = 5      # Recent tasks only
MAX_SNAPSHOT_TOKENS = 500      # Hard limit, truncate if exceeded
```

**3. Refresh Strategy**

- Rebuild snapshot on session start
- Rebuild after any mutation tool call
- Include `last_updated` timestamp to detect staleness

**4. Fallback to Tools for Complex Queries**

```python
QUERY_TOOLS = [
    {
        "name": "query_tasks",
        "description": "Query tasks with filters. Use when user needs: filtered lists, counts, analytics, or tasks outside current snapshot.",
        "input_schema": {
            "properties": {
                "project": {"type": "string"},
                "status": {"type": "string"},
                "executor": {"type": "string"},
                "since": {"type": "string", "description": "ISO date"}
            }
        }
    }
]
```

### Next Steps for Implementation

1. **Phase 1**: Implement `build_context_snapshot()` function
2. **Phase 2**: Integrate snapshot into coordinator's system prompt
3. **Phase 3**: Add refresh logic after mutations
4. **Phase 4**: Test with real chat sessions, measure token savings
5. **Phase 5**: Add pagination/summarization for scale

---

## Appendix: Sources

- [LangGraph Persistence Guide (Fastio)](https://fast.io/resources/langgraph-persistence/)
- [AI Agent Orchestration Patterns (DevRev)](https://devrev.ai/blog/ai-agent-orchestration)
- [What Is AutoGen? A Practical 2026 Guide (Nerova)](https://nerova.ai/guides/what-is-autogen-practical-guide-2026)
- [CrewAI GitHub Repository](https://github.com/crewaiinc/crewai)
- [Managing shared state across crewAI tasks (GitHub Discussion)](https://github.com/crewAIInc/crewAI/discussions/4111)
- [What is Agentic RAG? (Neo4j)](https://neo4j.com/blog/agentic-ai/what-is-agentic-rag/)
- [How to optimize token efficiency in agentic systems (Glean)](https://www.glean.com/perspectives/how-to-optimize-token-efficiency-in-agentic-systems)
- [Agentic RAG Architecture Patterns for Enterprise AI (Dedicatted)](https://dedicatted.com/insights/agentic-rag-architecture-patterns-that-actually-work-for-enterprise-ai)
