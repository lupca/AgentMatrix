# Control Tower V2

Hệ thống điều phối coding agent với gate-based workflow, four-eyes review, và autonomy controls.

## Overview

Control Tower V2 orchestrates coding agents across a gate-based workflow. Tasks move
through a well-defined lifecycle (todo → dispatched → awaiting-review → in-review →
done/changes-requested/failed), with every transition guarded by an append-only gate
ledger and enforced four-eyes review (reviewer ≠ executor).

## Architecture

```
Frontend (React/Vite/Tailwind)     Backend (FastAPI/SQLAlchemy)     Worker (Dramatiq)
├── Dashboard, Kanban              ├── /api/chat (SSE)              ├── run_agent
├── Tasks, Agents, Projects        ├── /api/tasks, /api/agents      ├── advance_task
└── WebSocket ← Redis pubsub       ├── CommandRouter                └── Redis broker
                                   ├── CoordinatorService
                                   ├── TaskOrchestrationService
                                   └── CLIDispatcher → claude/agy/codex
```

## Key Concepts

- **Task**: the unit of work tracked through the gate-based state machine.
- **AgentRun**: a single execution or review pass by a CLI-backed agent.
- **GateRecord**: an append-only, immutable ledger of gate decisions.
- **Agent**: CLI-backed (claude/agy/codex) with capabilities and a success rate.
- **Session**: conversational context scoped to global, project, or task level.

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
