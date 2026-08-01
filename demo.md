# Control Tower V2

Hệ thống điều phối coding agent với gate-based workflow, four-eyes review, và autonomy controls.

## Tổng quan

Control Tower V2 điều phối các coding agent (claude/agy/codex) thông qua một quy trình dựa trên gate:
dispatch → execute → review → verdict. Mỗi bước được ghi lại trong một ledger bất biến (`GateRecord`)
và yêu cầu reviewer khác với executor (four-eyes).

## Kiến trúc

```
Frontend (React/Vite/Tailwind)     Backend (FastAPI/SQLAlchemy)     Worker (Dramatiq)
├── Dashboard, Kanban              ├── /api/chat (SSE)              ├── run_agent
├── Tasks, Agents, Projects        ├── /api/tasks, /api/agents      ├── advance_task
└── WebSocket ← Redis pubsub       ├── CommandRouter                └── Redis broker
                                   ├── CoordinatorService
                                   ├── TaskOrchestrationService
                                   └── CLIDispatcher → claude/agy/codex
```

## Data Model chính

- **Task**: todo→dispatched→awaiting-review→in-review→done/changes-requested/failed
- **AgentRun**: queued→running→success/failed/timeout (kind: execute|review)
- **GateRecord**: append-only ledger (pending→approved/rejected)
- **Agent**: CLI-backed (claude/agy/codex) với capabilities[], success_rate
- **Session**: context_level (global|project|task), messages[]

## Gate Flow

```
todo → [dispatch gate] → dispatched → [run_agent] → awaiting-review
     → [review_order gate] → in-review → [run_agent review] → [verdict gate] → done
```

Mode: supervised (cần approve), plan-only (block dispatch), bypass (auto-approve)

## Autonomy & Brakes

- `autonomy_enabled=false` → STOP
- `task_cost ≥ max_cost_usd_per_task` → STOP
- `active_runs ≥ max_concurrent_runs` → QUEUE

## Quy tắc quan trọng

- **Four-eyes**: reviewer ≠ executor (DB constraint)
- **GateRecord**: append-only, immutable
- **Worktree isolation**: mỗi AgentRun chạy trong git worktree riêng
- **MCP integration**: CLI agent gọi CT tools qua MCP server

## Getting Started

```bash
# Dev
./scripts/start-backend.sh      # FastAPI + Dramatiq worker
cd frontend && npm run dev      # Vite dev server

# Test
pytest backend/tests/ -v

# Docker
docker-compose up --build
```
