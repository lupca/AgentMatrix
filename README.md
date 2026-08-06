# Control Tower V2

Task coordination system for coding agents with gate-based workflow, four-eyes review, and autonomy controls.

## Architecture

| Component | Stack | Port / Interface |
|-----------|-------|------------------|
| MCP Server (Native) | FastMCP (Python) | 8100 (HTTP/SSE) |
| Worker | Dramatiq + Redis | - |
| Database | PostgreSQL | 5433 (host) / 5432 (container) |
| Cache & Outbox | Redis | 6380 (host) / 6379 (container) |

## Quick Start (5 Steps)

```bash
# 1. Clone repo & set environment
git clone <repo-url> && cd agenticmatix
cp .env.example .env   # edit MCP_TOKEN_SECRET if needed

# 2. Start services (PostgreSQL, Redis, FastMCP Server, Dramatiq Worker)
./scripts/start-backend.sh

# 3. Setup read-only database role (for query_db tool)
./scripts/create-readonly-role.sh

# 4. Initialize coordinator workspace outside the repo
./scripts/init-coordinator-workdir.sh ~/ct-coordinator

# 5. Launch coordinator CLI inside the workdir
cd ~/ct-coordinator && claude  # or agy / codex
```

## Useful Scripts

| Script | Purpose |
|--------|---------|
| `./scripts/start-backend.sh` | Starts Docker services (DB/Redis) and launches FastMCP server + Dramatiq worker |
| `./scripts/create-readonly-role.sh` | Provisions the `ct_readonly` Postgres role and `ct_readonly_user` |
| `./scripts/init-coordinator-workdir.sh <dir>` | Creates isolated workspace for coordinator CLI runs |

## Development & Testing

```bash
# Run tests using the unified virtual environment
backend/venv/bin/python -m pytest backend/tests -q
```

## Documentation

The spec, the work log, the evidence and the raw ideas all live in the
`control_tower` database and are read through the MCP tool surface, not in
markdown files:

| Question | Tool |
|---|---|
| How must the system behave? | `spec_get` / `spec_write` (spec_item, with code anchors) |
| What is being worked on? | `get_status`, `query_db` on `tasks` |
| Evidence, measurements, incident logs | `manage_knowledge` |
| Raw ideas not yet a task | `manage_inbox` |

Only two markdown files remain, because something reads them:

- `CLAUDE.md` - system reference for AI assistants
- `docs/coordinator-rules.md` - source of the coordinator workspace instruction
  files, copied by `scripts/init-coordinator-workdir.sh`
- `docs/AGENT-PLAYBOOK.md` - distilled working memory for an agent starting a
  session

Everything else was deleted on 2026-08-06 (it is still in git history) after
its live content was moved into the database.
