#!/bin/bash
# Start Control Tower V2 Backend locally
# DB and Redis still run in Docker

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_DIR/backend"
PID_FILE="$PROJECT_DIR/.backend.pid"
LOG_FILE="$PROJECT_DIR/backend.log"
WORKER_PID_FILE="$PROJECT_DIR/.worker.pid"
WORKER_LOG_FILE="$PROJECT_DIR/worker.log"

# Check if already running
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Backend already running (PID: $PID)"
        exit 0
    fi
    rm "$PID_FILE"
fi

if [ -f "$WORKER_PID_FILE" ]; then
    WORKER_PID=$(cat "$WORKER_PID_FILE")
    if ! kill -0 "$WORKER_PID" 2>/dev/null; then
        rm "$WORKER_PID_FILE"
    fi
fi

# Ensure DB and Redis are running
echo "Starting DB and Redis..."
cd "$PROJECT_DIR"
docker compose up -d db redis
sleep 2

# Wait for DB to be healthy
echo "Waiting for DB..."
until docker compose exec -T db pg_isready -U ct -d control_tower 2>/dev/null; do
    sleep 1
done

# Load project-level env (MCP_TOKEN_SECRET, ...). The server starts from
# backend/, so pydantic's CWD-relative `.env` lookup misses the root file —
# it must be exported into the environment here.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

# Set environment (script-managed values win over .env for local docker ports)
export DATABASE_URL="postgresql://ct:secret@localhost:5433/control_tower"
export REDIS_URL="redis://localhost:6380/0"

if [ -z "${MCP_TOKEN_SECRET:-}" ]; then
    echo "MCP_TOKEN_SECRET is not set (checked environment and $PROJECT_DIR/.env)" >&2
    exit 1
fi

# Install deps if needed
cd "$BACKEND_DIR"
if [ ! -d "venv" ]; then
    echo "Creating venv..."
    python3 -m venv venv
fi

echo "Installing dependencies..."
source venv/bin/activate
pip install -r requirements.txt -q

# Run migrations
echo "Running migrations..."
alembic upgrade head

# Start backend
echo "Starting native MCP server on port 8100..."
nohup python -m app.mcp_native --host 0.0.0.0 --port 8100 > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# Start worker
echo "Starting Dramatiq worker..."
# Cap worker processes: dramatiq defaults to one process per CPU core, and
# each process holds its own SQLAlchemy pool (up to 15 conns) — on a many-core
# machine that alone exhausts Postgres max_connections. Concurrency of agent
# runs is governed by the max_concurrent_runs brake, not worker count.
nohup dramatiq app.workers.agent_runner app.workers.outbox_publisher --processes 2 --threads 4 > "$WORKER_LOG_FILE" 2>&1 &
echo $! > "$WORKER_PID_FILE"

sleep 3
# Retry health check a few times
for i in 1 2 3 4 5; do
    if curl -s http://localhost:8100/health > /dev/null; then
        break
    fi
    sleep 1
done

if curl -s http://localhost:8100/health > /dev/null; then
    echo "Backend started successfully (PID: $(cat $PID_FILE))"
    echo "Worker started successfully (PID: $(cat $WORKER_PID_FILE))"
    echo "Log: $LOG_FILE"
else
    echo "Backend failed to start. Check $LOG_FILE"
    cat "$LOG_FILE" | tail -20
    exit 1
fi
