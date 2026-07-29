# Tool System Architecture Research

> Research document for CTV2-076
> Date: 2026-07-27

## Tool Audit

### 1. API Mode Tools (tool_definitions.py)

**EAGER_TOOLS** (loaded in every conversation context):

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `pm_create_task` | Create a new task | `title` (required), `project` (optional) |
| `get_status` | Get task status or list recent tasks | `task_id` (optional) |

**DEFERRED_TOOLS** (loaded on-demand via Anthropic tool search):

| Tool Name | Description | Parameters |
|-----------|-------------|------------|
| `dispatch_task` | Assign executor to task | `task_id` (required), `executor` (optional) |
| `record_verdict` | Record review verdict (pass/changes) | `task_id`, `verdict`, `findings[]` |
| `approve_gate` | Approve pending gate | `task_id` |
| `cancel_task` | Cancel a task | `task_id` |
| `compact_context` | Summarize older session messages | (none) |

**Supporting Tools**:
- `tool_search_tool_regex` - Required for deferred tool discovery

### 2. Slash Commands (command_router.py)

| Slash Command | Handler | Maps to Tool |
|---------------|---------|--------------|
| `/pm` | `create_task` | `pm_create_task` |
| `/dispatch` | `dispatch_task` | `dispatch_task` |
| `/verdict` | `verdict` | `record_verdict` |
| `/approve` | `approve_gate` | `approve_gate` |
| `/status` | `get_status` | `get_status` |
| `/cancel` | `cancel_task` | `cancel_task` |
| `/compact` | `compact_context` | `compact_context` |
| `/help` | `show_help` | (no tool equivalent) |

The `CommandRouter.execute_tool()` method bridges tool calls to slash command handlers.

### 3. REST API Endpoints

**Tasks API** (`/api/tasks`):
- `POST /api/tasks` - Create task
- `GET /api/tasks` - List tasks (with filters)
- `GET /api/tasks/{id}` - Get task details
- `PATCH /api/tasks/{id}` - Update task
- `GET /api/tasks/{id}/history` - Get audit log
- `GET /api/tasks/{id}/messages` - Get session messages
- `GET /api/tasks/{id}/runs` - Get agent runs
- `GET /api/tasks/{id}/suggested-agents` - Get agent suggestions
- `POST /api/tasks/{id}/review` - Request review
- `POST /api/tasks/{id}/verdict` - Submit verdict

**Dispatch API** (`/api/dispatch`, `/api/gates`):
- `POST /api/dispatch` - Dispatch agent to task
- `GET /api/dispatch/{run_id}` - Get run status
- `POST /api/dispatch/{run_id}/cancel` - Cancel run
- `POST /api/gates/{id}/decision` - Approve/reject gate

**Chat API** (`/api/chat`):
- `POST /api/chat` - Send message (handles slash commands + LLM)

### 4. MCP Tools (code-review-graph)

The MCPClient (`mcp.py`) connects to `code-review-graph serve`:

| Tool Category | Examples |
|---------------|----------|
| Graph Building | `build_or_update_graph_tool`, `embed_graph_tool` |
| Querying | `query_graph_tool`, `traverse_graph_tool`, `semantic_search_nodes_tool` |
| Analysis | `get_affected_flows_tool`, `get_impact_radius_tool`, `find_large_functions_tool` |
| Review | `get_review_context_tool`, `detect_changes_tool` |

### 5. CLI Mode Tools

External CLIs are dispatched via `cli_dispatcher.py`:

| CLI | Provider | Command Format |
|-----|----------|----------------|
| `claude` | Anthropic | `claude --model {model} -p {prompt}` |
| `agy` | Google | `agy --agent {model} --print {prompt}` |
| `codex` | OpenAI | `codex exec -m {model} {prompt}` |

**Important**: CLI mode does NOT expose Control Tower tools directly. The CLI receives formatted conversation history and responds as a general assistant. Slash commands bypass the CLI entirely.

---

## CLI Analysis

### How Slash Commands Work

1. User enters `/pm Create my task` in chat UI
2. `POST /api/chat` receives the message
3. `CommandRouter.parse()` extracts command (`/pm`) and args (`Create my task`)
4. Since command is recognized, `CommandRouter.execute()` handles it directly
5. Result is returned as JSON through SSE stream
6. **No LLM/CLI is invoked** - zero tokens consumed

### How LLM Messages Work

1. User enters natural language in chat UI
2. `POST /api/chat` receives the message
3. `CommandRouter.parse()` returns `None` (no command)
4. `CoordinatorService.stream_turn()` is called
5. `ContextHierarchy.build_messages()` constructs context:
   - Global system prompt (`global_context.md`)
   - Project context (if applicable)
   - Task context (if applicable)
   - Session message history
6. Tool definitions injected via `get_tool_definitions()`
7. CLI dispatcher formats history as prompt and spawns CLI process
8. Response streamed back to UI

### Tool Schema Delivery

For API-mode providers (OpenAI adapter):
- Tools passed to `adapter.complete()` via `tools=ctx.get_tool_definitions()`
- Model can invoke tools; results executed via `CommandRouter.execute_tool()`

For CLI-mode (claude/agy):
- Tool schemas are NOT passed to the CLI
- The CLI uses its built-in tool system (Bash, Read, Write, etc.)
- Control Tower tools must be invoked via slash commands or the chat API

### What the Chat UI Shows

The `ChatPanel.tsx` component:
- Displays tool execution status (`tool_call`, `tool_result` events)
- Shows tool name, arguments, and results inline
- Does NOT show a tool palette or command autocomplete
- Users must know slash commands or phrase requests naturally

---

## Architecture Proposal

### Problem Statement

Current issues with scattered tool definitions:

1. **No single source of truth** - Tools defined in 4+ locations
2. **Naming inconsistency** - `pm_create_task` vs `/pm` vs `POST /api/tasks`
3. **No user discovery** - Chat UI doesn't help users find available tools
4. **CLI mode gap** - Control Tower tools not available in CLI sessions
5. **Maintenance burden** - Changes require updates in multiple places

### Proposed Solution: MCP Server for Control Tower

Create a dedicated MCP server (`control-tower-mcp`) that exposes all task management tools:

```
control-tower-v2/
├── mcp-server/
│   ├── src/
│   │   ├── tools.ts        # Tool definitions (single source of truth)
│   │   ├── handlers.ts     # Tool implementation (calls REST API)
│   │   └── server.ts       # MCP stdio server
│   └── package.json
```

#### Benefits

1. **Single source of truth** - All tools defined in one place with MCP schema
2. **CLI integration** - Claude Code can use Control Tower tools via MCP
3. **Tool discovery** - MCP `tools/list` provides discoverability
4. **Consistent naming** - One canonical name per tool
5. **Ecosystem alignment** - Follows Claude Code's MCP architecture

#### Tool Schema Design

```typescript
const TOOLS = {
  create_task: {
    description: "Create a new task in Control Tower",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", description: "Task title" },
        project: { type: "string", description: "Project ID (optional)" },
      },
      required: ["title"],
    },
  },
  get_status: {
    description: "Get task status or list recent tasks",
    inputSchema: {
      type: "object",
      properties: {
        task_id: { type: "string", description: "Task ID (optional)" },
      },
    },
  },
  dispatch_task: {
    description: "Dispatch an agent to execute a task",
    inputSchema: {
      type: "object",
      properties: {
        task_id: { type: "string" },
        agent_id: { type: "string" },
      },
      required: ["task_id"],
    },
  },
  // ... remaining tools
};
```

#### Migration Path

1. **Phase 1**: Create MCP server with all Control Tower tools
2. **Phase 2**: Generate `tool_definitions.py` from MCP schemas
3. **Phase 3**: Update `CommandRouter` to delegate to MCP server
4. **Phase 4**: Add tool palette UI to chat interface
5. **Phase 5**: Deprecate direct slash command handling

#### CLI Integration

Add to `.claude/settings.json`:
```json
{
  "mcpServers": {
    "control-tower": {
      "type": "stdio",
      "command": "control-tower-mcp",
      "args": ["--api-url", "http://localhost:8000"]
    }
  }
}
```

### Alternative Approaches Considered

#### Option A: YAML-based Tool Registry

```yaml
# tools.yaml
tools:
  - name: create_task
    slash: /pm
    endpoint: POST /api/tasks
    description: Create a new task
    parameters:
      - name: title
        type: string
        required: true
```

**Pros**: Simple, static file
**Cons**: Requires code generation, no runtime discovery, doesn't solve CLI gap

#### Option B: OpenAPI-First

Generate tool schemas from OpenAPI spec.

**Pros**: Aligns with REST API
**Cons**: Not all tools map cleanly to REST, still doesn't solve CLI integration

### Recommendation

**Implement MCP Server approach** for the following reasons:

1. Aligns with existing MCP infrastructure (code-review-graph)
2. Native support in Claude Code CLI
3. Provides tool discovery out of the box
4. Enables gradual migration without breaking existing code
5. Future-proof for multi-agent scenarios

### Immediate Actions

1. Create `mcp-server/` directory structure
2. Define tool schemas in TypeScript
3. Implement HTTP client to call Control Tower REST API
4. Test with Claude Code CLI
5. Document MCP server setup in README

---

## References

- `backend/app/services/tool_definitions.py` - Current tool schemas
- `backend/app/services/command_router.py` - Slash command handlers
- `backend/app/services/coordinator.py` - Coordinator service
- `backend/app/services/cli_dispatcher.py` - CLI dispatch
- `backend/app/services/mcp.py` - MCP client for code-review-graph
- `frontend/src/components/chat/ChatPanel.tsx` - Chat UI
