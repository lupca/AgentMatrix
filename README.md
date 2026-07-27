# Control Tower V2

Control Tower V2 is a redesigned task coordination and management system built with Python, FastAPI, LangGraph, Chainlit, and Streamlit.

## Architecture & Services

The application consists of four main Docker container services:

- **`db`**: PostgreSQL 16 database (Source of Truth)
- **`backend`**: FastAPI REST API + Alembic migrations
- **`chat`**: Chainlit interactive Chat UI
- **`dashboard`**: Streamlit Task Monitoring Dashboard

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (v20.10+)
- [Docker Compose](https://docs.docker.com/compose/) (v2.0+ or `docker-compose`)

## Environment Setup

Copy `.env.example` to `.env` and configure your environment variables:

```bash
cp .env.example .env
```

Key environment variables:
- `POSTGRES_USER`: Database user (default: `ct`)
- `POSTGRES_DB`: Database name (default: `control_tower`)
- `DB_PASSWORD`: Database password
- `POSTGRES_PORT`: Host port mapped for PostgreSQL (default: `15436`)
- `BACKEND_PORT`: Host port mapped for FastAPI backend (default: `18000`)
- `CHAT_PORT`: Host port mapped for Chainlit chat UI (default: `18080`)
- `DASHBOARD_PORT`: Host port mapped for Streamlit dashboard (default: `18501`)
- `ANTHROPIC_API_KEY`: API key for Claude integration

## Deployment

### Using Deployment Script

Run the automated deployment script:

```bash
./scripts/deploy.sh
```

### Using Docker Compose Directly

To build and start all containers in detached mode:

```bash
docker-compose up --build -d
```

or with Docker CLI v2:

```bash
docker compose up --build -d
```

## Service URLs

Once all containers are running and healthy:

- **Backend API & Health**: [http://localhost:18000](http://localhost:18000) (OpenAPI Docs: [http://localhost:18000/docs](http://localhost:18000/docs))
- **Chainlit Chat UI**: [http://localhost:18080](http://localhost:18080)
- **Streamlit Dashboard**: [http://localhost:18501](http://localhost:18501)
- **PostgreSQL**: `localhost:15436`

*(Host ports are configurable in `.env`)*

## Verification & Health Status

Check the status of running containers and health checks:

```bash
docker-compose ps
```

Verify individual service health endpoints:

```bash
# Backend Health Endpoint
curl http://localhost:18000/health

# Chat UI Endpoint
curl http://localhost:18080/

# Dashboard Endpoint
curl http://localhost:18501/_stcore/health
```

## Coordinator Chat CLI: MCP Tool Access

When a chat turn is routed to an account-backed CLI (`claude`, `codex`, or
`agy`) instead of the OpenAI-compatible API, that CLI can reach the same
Control Tower tools (create/dispatch tasks, `query_db`, admin actions, ...)
through an MCP stdio server, `backend/app/mcp_server.py`. It's a thin
projection of the tool registry (`backend/app/services/tool_registry.py`) —
one canonical name and schema per tool, shared with API mode — whose
handlers call the backend over `POST /api/mcp/tools/call` with a scoped
bearer token. Permission and gate checks run server-side in that endpoint
(the same `CommandRouter.execute_tool` path API mode uses), so a CLI can
never bypass the four-eyes rule locally.

The **executor dispatch** CLI (`agent_runner`, which runs a CLI inside a
target repo to write code) is unrelated and unaffected: it never gets
Control Tower tools.

### Enable it

1. Set a scoped token the backend and the MCP server both use:

   ```bash
   MCP_API_TOKEN=<a long random string>
   ```

2. `backend/app/services/cli_dispatcher.py` picks this up automatically
   (via `MCP_API_TOKEN` / `CT_API_URL`) and passes a generated `--mcp-config`
   file to the CLI on every coordinator chat turn. No token configured means
   no `--mcp-config` flag — CLI-mode behaves exactly as before.

### Config shape

`build_mcp_config()` generates one file per CLI spawn in the standard
`mcpServers` shape:

```json
{
  "mcpServers": {
    "control-tower": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "app.mcp_server", "--api-url", "http://localhost:8000"],
      "env": { "CT_MCP_TOKEN": "<scoped token>" }
    }
  }
}
```

`claude`, `codex`, and `agy` all read this file via `--mcp-config <path>`.
To register it manually against a running backend (for local testing
outside the coordinator):

```bash
cd backend
CT_MCP_TOKEN=<scoped token> python -m app.mcp_server --api-url http://localhost:8000
```

## Shutdown & Cleanup

To stop and remove all services without deleting persistent volumes:

```bash
docker-compose down
```

To stop all services and remove database volumes (clean shutdown):

```bash
docker-compose down -v
```
