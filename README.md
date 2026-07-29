# Control Tower V2

Task coordination system for coding agents with gate-based workflow, four-eyes review, and autonomy controls.

## Architecture

| Component | Stack | Port |
|-----------|-------|------|
| Backend API | FastAPI + SQLAlchemy | 8000 |
| Frontend | React + Vite + Tailwind + shadcn/ui | 5173 |
| Worker | Dramatiq + Redis | - |
| Database | PostgreSQL | 5432 |

## Quick Start

```bash
# 1. Database
docker-compose up -d db

# 2. Backend + Worker
./scripts/start-backend.sh

# 3. Frontend
cd frontend && npm install && npm run dev
```

## Service URLs

- Backend API: http://localhost:8000 (OpenAPI: /docs)
- Frontend: http://localhost:5173
- PostgreSQL: localhost:5432

## Environment

Copy `.env.example` to `.env`:

```bash
DATABASE_URL=postgresql://ct:password@localhost:5432/control_tower
REDIS_URL=redis://localhost:6379
MCP_API_TOKEN=<random string>  # enables CLI→MCP tool access
```

## MCP Integration

CLI coordinators (claude/agy/codex) access CT tools via MCP:

```json
{
  "mcpServers": {
    "control-tower": {
      "command": "python",
      "args": ["-m", "app.mcp_server", "--api-url", "http://localhost:8000"],
      "env": {"CT_MCP_TOKEN": "<token>"}
    }
  }
}
```

Set `MCP_API_TOKEN` in `.env` to enable. CLIDispatcher auto-generates config per spawn.

## Key Concepts

- **Gates**: spec → dispatch → review_order → verdict
- **Modes**: supervised (human approve), plan-only (block dispatch), bypass (auto)
- **Four-eyes**: reviewer must differ from executor
- **Brakes**: autonomy toggle, cost limit, concurrency limit

## Development

```bash
# Run tests
pytest backend/tests/ -v

# Migrations
cd backend && alembic upgrade head

# Full Docker stack
docker-compose up --build
```

## Documentation

- `CLAUDE.md` - System spec for AI assistants
- `docs/adr/ADR-001-unified-tool-architecture.md` - Tool system design
- `docs/design/` - Architecture designs
- `docs/research/` - Research docs
