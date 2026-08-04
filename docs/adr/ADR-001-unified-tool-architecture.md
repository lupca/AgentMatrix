# ADR-001: Unified Tool Architecture & Full DB Access

> Status: **Implemented** (Phases 1-3 complete)
> Date: 2026-07-27
> Updated: 2026-07-29
> Related: CTV2-075, CTV2-076, CTV2-059
> Supersedes: MCP-TypeScript-first proposal in `docs/research/tool-system-architecture.md`

## Implementation Status

| Phase | Status | Notes |
|-------|--------|-------|
| 1a | ✅ Done | `tool_registry.py` + CommandRouter + `/api/tools` |
| 1b | ✅ Done | Context layout fix, snapshot placement |
| 1c | ✅ Done | Legacy adapter removal |
| 2a | ✅ Done | System State snapshot + `query_db` |
| 2b | ✅ Done | `load_tools` meta-tool |
| 2c | ✅ Done | `manage_project/agent/knowledge` + gate wiring |
| 2d | ✅ Done | `Settings` table + `update_settings` |
| 3 | ✅ Done | MCP projection (`app/mcp_server.py`) + CLI config |
| 4 | Partial | UI tool palette (basic, not full registry dump) |

## 1. Context & Constraints

Provider decision (2026-07): **Claude SDK and Antigravity/Google SDK adapters are dropped.**
The coordinator now runs on exactly two paths:

- **API mode** — OpenAI-compatible Chat Completions (`OpenAIAdapter`), with native
  function calling and the tool-execution loop in `CoordinatorService` (max 20 iterations).
- **CLI mode** — external CLIs (`claude`, `agy`, `codex`) spawned per turn with the
  session history formatted as a single prompt. No AGENTMATRIX tools are passed.

Goals of this ADR:

1. One consistent tool system across API mode, CLI mode, slash commands, and UI (unification).
2. Reduce tokens per turn and improve task quality (project-level goal).
3. Extend coordinator capabilities to **full DB access** (Projects, Agents, Sessions,
   Knowledge, Settings) — today only the Task lifecycle has tools.

## 2. Current Architecture (audited 2026-07-27)

### 2.1 Tiered context (`context_hierarchy.py` + `graph/context.py`)

```
Tier 1 GLOBAL   global_context.md  +  <context snapshot appended>   [cache_control]
Tier 2 PROJECT  description + context_md + auto-memory (cap 25KB)   [cache_control]
Tier 3 TASK     task header + LangGraph gate state + session messages (dynamic)
```

- The snapshot (`build_context_snapshot`) lists active projects + task counts + 5 recent
  tasks; it is invalidated on every mutation (`invalidate_context_snapshot`).
- `budget_messages` pins "system or has cache_control" messages as the stable prefix.

### 2.2 Tool paths

| Path | Source of definitions | Execution | Notes |
|---|---|---|---|
| API mode | `tool_definitions.py` (2 eager + 5 deferred) | `CommandRouter.execute_tool` → slash handlers | tool loop in coordinator |
| Slash commands | `COMMANDS` dict in `command_router.py` | same handlers, **0 LLM tokens** | intercepted before LLM |
| REST API | `app/api/*.py` | service layer (`TaskOrchestrationService`) | UI + external callers |
| CLI mode | — (none) | CLI built-ins only | CT tools unavailable |
| MCP (code-review-graph) | external server | read-only static analysis | unrelated to CT CRUD |

### 2.3 LangGraph

The gate pipeline (`graph/builder.py`) is a deterministic state machine
(parse → spec → approval → plan → dispatch → review-order → verdict → sync/log) with a
Postgres checkpointer. Tools/commands mutate DB through `TaskOrchestrationService`;
`ContextHierarchy` reads live gate state back from the checkpointer into Tier 3.
Tool calls are **not** LangGraph nodes — the graph governs task state, not conversation.

## 3. Problems Found

**P1 — Deferred loading is a silent no-op on OpenAI.**
`defer_loading: True` and `tool_search_tool_regex_20251119` are Anthropic-API features.
`OpenAIAdapter.render_tools()` drops the tool-search declaration (no `input_schema`) but
renders every "deferred" tool as a normal function tool. Result: all 7 schemas are sent
eagerly on every request; the "~10% context saving" comment in `tool_definitions.py` is
false in the OpenAI-only world. This matters more as tool count grows with DB access.

**P2 — Snapshot placement busts OpenAI prefix cache.**
`cache_control: ephemeral` markers are Anthropic-only and are dropped by the adapter.
OpenAI caches by **longest stable prefix** (tools serialize before messages). The dynamic
snapshot is appended to the *end of the Tier-1 global message*, so any project/task
mutation invalidates the cached prefix including tool schemas and the Project tier
(up to 25KB) behind it.

**P3 — Dead/legacy provider code.** `AnthropicAdapter`/`GoogleAdapter` remain the
`ProviderRouter` defaults; `DEFAULT_CONTEXT_WINDOWS` only knows claude/gemini;
`_resolve_selection` carries a legacy-adapter compatibility path. Cognitive and
maintenance overhead with no runtime value.

**P4 — CLI-mode capability gap.** A CLI coordinator turn cannot execute any CT tool;
mutations silently depend on the user typing slash commands.

**P5 — No single source of truth.** Tool names/behavior defined in 4 places
(`tool_definitions.py`, `COMMANDS` dict, `execute_tool` translation table, REST
endpoints); naming drifts (`pm_create_task` / `/pm` / `POST /api/tasks`). The UI has no
tool palette; discoverability is zero.

**P6 — DB coverage gap.** Tools exist only for Task lifecycle. No tool for Project CRUD,
Agent CRUD, KnowledgeItem, Session management, Stats/LLMUsage reads. There is **no
Settings entity at all** in `db/models.py` — `update_settings` from the sketch has no
backing model today.

## 4. Decision

### D1 — Python Tool Registry as the single source of truth

One module, `backend/app/services/tool_registry.py`, declaring every tool exactly once:

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                 # canonical name, e.g. "create_task"
    description: str
    parameters: dict          # JSON Schema (OpenAI function format)
    handler: str              # dotted path or callable in service layer
    tier: Literal["eager", "deferred"]
    permission: Literal["read", "write", "admin"]
    entity: str               # tasks|projects|agents|sessions|knowledge|settings
    slash_alias: str | None   # "/pm" etc. — slash = alias, not a separate system
    group: str                # for load_tools(): "task_lifecycle"|"admin"|"query"|...
```

Everything else becomes a **projection** of the registry:

- `get_tool_definitions()` → OpenAI function schemas (eager set + meta-tool).
- `CommandRouter` → thin parser: `/x args` → registry lookup by `slash_alias` → same handler.
- `GET /api/tools` → registry dump for the chat UI tool palette + `/help`.
- MCP server (Phase 3) → FastMCP (Python, same repo) exposing the registry over stdio.

The TypeScript MCP server proposed in `tool-system-architecture.md` is **rejected as the
SSoT**: with an OpenAI-only API mode and a Python backend, a TS server adds a
cross-language hop, codegen, and a second deploy unit. MCP remains — but as a thin
Python projection for CLI integration, not as the definition point.

### D2 — Hybrid read/write split, extended to the whole DB (per CTV2-059)

**Reads = snapshot, writes = tools.** Expand the snapshot with a System State block:

```
## System State
- Projects: 5 active (topvnsport-pmi, control-tower-v2, ...)
- Agents: 8 configured (3 api / 5 cli; default: gpt-x)
- Sessions: 3 active
- Tasks: 12 open (3 dispatched, 1 in-review, 1 awaiting approval)
[project scope] Recent tasks in <project>: ... (unchanged)
```

Hard cap ~30 lines / ~600 tokens; names truncated; counts always, enumerations top-N.
Long-tail reads go through **one generic deferred tool** instead of N list tools:

```
query_db(entity: tasks|projects|agents|sessions|knowledge|usage,
         filters: {status?, project?, q?, ...}, limit<=50, offset?)
```

Read-only, whitelisted entities and filter fields, serialized compactly (id/title/status
columns, not full rows). One schema (~150 tokens) replaces 6+ per-entity tools.

**Writes** get consolidated action-enum tools (mirrors the sketch, with guardrails):

| Tool | Tier | Perm | Notes |
|---|---|---|---|
| `create_task` (rename of `pm_create_task`) | eager | write | alias `/pm` |
| `get_status` | eager | read | alias `/status`; keep — hot path |
| `query_db` | eager | read | generic reads |
| `load_tools(group)` | eager | read | meta-tool, see D3 |
| `dispatch_task` | deferred (task_lifecycle) | write | alias `/dispatch` |
| `record_verdict` | deferred (task_lifecycle) | write | alias `/verdict` |
| `approve_gate` | deferred (task_lifecycle) | write | alias `/approve` |
| `cancel_task` | deferred (task_lifecycle) | write | alias `/cancel` |
| `update_task(task_id, patch)` | deferred (task_lifecycle) | write | plan/AC/priority edits |
| `manage_project(action: create\|update\|archive, ...)` | deferred (admin) | admin | **no hard delete** — archive only |
| `manage_agent(action: create\|update\|disable, ...)` | deferred (admin) | admin | never accepts/returns `api_key` value |
| `manage_knowledge(action: create\|update\|archive, ...)` | deferred (admin) | write | KnowledgeItem CRUD |
| `update_settings(key, value)` | deferred (admin) | admin | requires new `Settings` KV table (migration) |
| `compact_context` | deferred (session) | write | alias `/compact` |

Guardrails: `delete` is not exposed as an LLM action anywhere — archive/disable instead;
destructive or admin-permission tools route through the existing gate flow
(`GateRecord` pending → `/approve`) when mode is `supervised`. All mutations keep going
through `TaskOrchestrationService`/service layer so DB constraints (four-eyes checks,
append-only gate records) hold for every entry point identically.

### D3 — OpenAI-compatible deferred loading via `load_tools` meta-tool

OpenAI has no server-side tool search. Emulate two-tier loading inside the existing
tool-execution loop:

1. Each request carries: 4 eager tools + `load_tools(group)` (~400–500 tokens total).
2. When the model calls `load_tools("admin")`, the handler returns the group's schema
   list **and** the coordinator appends those schemas to the `tools` array for the
   remaining iterations of *this turn only*.
3. Next turn resets to the eager set → the request prefix stays byte-stable → prefix
   cache keeps hitting.

This restores the tiering that P1 lost, and scales: adding entity tools grows the
deferred groups, not the per-turn baseline. The system prompt keeps a one-line hint:
"More tools available via load_tools(group): task_lifecycle, admin, session."

### D4 — Cache-aware context layout

Reorder so volatility strictly increases down the request:

```
[tools]           eager schemas (stable)                      ← cached
system  Tier 1    global_context.md (static)                  ← cached
user    Tier 2    project context (semi-stable, 25KB cap)     ← cached
system  Tier 2.5  context snapshot incl. System State (dynamic) ← NOT cached
system  Tier 3    task header + gate state (dynamic)
...               session messages
```

- Remove the snapshot append from the Tier-1 message (`build_messages`), emit it as its
  own message after the Project tier.
- Replace the `cache_control`-presence check in `budget_messages` with an explicit
  `pinned: True` flag on prefix messages; drop `cache_control` emission entirely.
- Expected effect: global prompt (~700 tok) + tool schemas (~500 tok) + project tier
  (up to ~6K tok) become a stable cached prefix across turns and across mutations,
  instead of being re-billed after every task change.

### D5 — CLI unification via MCP projection (Phase 3)

There are **two distinct CLI paths** and only one of them is in scope here:

1. **Executor dispatch CLI** (`agent_runner` → CLI process in the *target repo*):
   writes code, uses the CLI's own built-in tools (Bash/Read/Write). It must NOT get
   AGENTMATRIX CRUD tools — its contract stays "do the work, report a result-ref".
   Unchanged by this ADR.
2. **Coordinator chat CLI** (chat UI turn routed to a CLI instead of the OpenAI API):
   needs the same CT tools as API mode (create task, query DB, dispatch, ...). This is
   the path the MCP projection serves.

For the coordinator chat CLI, expose the same registry through a FastMCP stdio
server (`python -m app.mcp_server --api-url http://localhost:8000`) registered in the
CLIs' MCP config (claude, codex, and agy all support MCP). Handlers call the REST API
with a scoped token; permissions and gates are enforced server-side, so the CLI path
can never bypass four-eyes. Until Phase 3 ships, CLI mode stays read-advisor +
user slash commands (current behavior, now documented instead of accidental).

### D6 — Legacy removal

Delete `anthropic_adapter.py`, `google_adapter.py`, the `ProviderRouter` legacy defaults
and `_explicit_provider_compatibility` seam; keep `route_model` for CLI routing only.
Remove `TOOL_SEARCH_TOOL` and `defer_loading` flags from `tool_definitions.py`
(replaced by the registry's `tier`/`group`).

## 5. Token Analysis (per 50-turn session, API mode)

| | Today (OpenAI, post-SDK-drop) | After D2–D4 |
|---|---|---|
| Tool schemas/turn | ~500 tok, all 7, cache broken by snapshot | ~450 tok eager, in stable cached prefix |
| Global+project prefix | re-billed after every mutation (P2) | cached across turns and mutations |
| Read queries (list agents/sessions/projects) | impossible or hallucinated → extra turns | 0 extra tokens (snapshot) or 1 `query_db` call |
| Long-tail tools | +N schemas/turn as coverage grows | +0 baseline; loaded per-turn on demand |

CTV2-059 measured 74–79% savings for hybrid-vs-pure-tools on Anthropic caching; the same
structure applies to OpenAI automatic prefix caching (typically 50–90% discount on cached
input, provider-dependent). Quality wins: no hallucinated system state (agents/sessions
now visible), one canonical name per tool in every mode, discoverable palette, identical
gate enforcement on every path.

## 6. Migration Plan

| Phase | Task | Depends |
|---|---|---|
| 1a | `tool_registry.py` + projections (`get_tool_definitions`, CommandRouter shim, `GET /api/tools`) — behavior-preserving refactor of existing 7 tools | — |
| 1b | D4 context layout fix (snapshot placement, `pinned` flag, drop cache_control) | — |
| 1c | D6 legacy adapter removal | 1a |
| 2a | Snapshot System State block + `query_db` | 1a |
| 2b | `load_tools` meta-tool in coordinator loop | 1a |
| 2c | `manage_project`, `manage_agent`, `manage_knowledge`, `update_task` + gate wiring for admin perm | 2a |
| 2d | `Settings` KV table migration + `update_settings` | 2c |
| 3 | FastMCP projection + CLI config + scoped API token | 2c |
| 4 | UI tool palette from `GET /api/tools`; deprecate direct COMMANDS dict | 1a |

Each phase is independently shippable; 1a/1b are pure refactors verifiable by existing
tests (`test_command_router.py`, `test_context_hierarchy.py`, `test_coordinator.py`).

## 7. Alternatives Considered

- **TypeScript MCP server as SSoT** (CTV2-076 proposal): rejected — cross-language hop,
  codegen chain, second deploy unit; OpenAI API mode can't consume MCP directly anyway.
  Retained as a thin Python projection (D5).
- **All-eager tools, no tiering**: acceptable at 7 tools, fails at 14+ (full DB access);
  every added entity permanently taxes every turn.
- **Per-entity list/get tools instead of `query_db`**: clearer schemas but linear token
  growth per entity; generic query with whitelists wins on tokens at equal quality.
- **MCP resources for reads**: no OpenAI-side support; snapshot already covers hot reads
  at zero marginal tokens.
- **LangGraph nodes as tools** (tool calls inside the gate graph): conflates conversation
  control with task state; current separation (graph = task FSM, loop = conversation)
  is simpler and already checkpointed.
