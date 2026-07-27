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

# Set environment
export DATABASE_URL="postgresql://ct:secret@localhost:5433/control_tower"
export REDIS_URL="redis://localhost:6380/0"

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
echo "Starting backend on port 8001..."
nohup uvicorn app.main:app --host 0.0.0.0 --port 8001 > "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"

# Start worker
echo "Starting Dramatiq worker..."
nohup dramatiq app.workers.agent_runner > "$WORKER_LOG_FILE" 2>&1 &
echo $! > "$WORKER_PID_FILE"

sleep 3
# Retry health check a few times
for i in 1 2 3 4 5; do
    if curl -s http://localhost:8001/health > /dev/null; then
        break
    fi
    sleep 1
done

if curl -s http://localhost:8001/health > /dev/null; then
    echo "Backend started successfully (PID: $(cat $PID_FILE))"
    echo "Worker started successfully (PID: $(cat $WORKER_PID_FILE))"
    echo "Log: $LOG_FILE"
else
    echo "Backend failed to start. Check $LOG_FILE"
    cat "$LOG_FILE" | tail -20
    exit 1
fi
