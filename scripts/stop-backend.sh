#!/bin/bash
# Stop Control Tower V2 Backend

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.backend.pid"
WORKER_PID_FILE="$PROJECT_DIR/.worker.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping backend (PID: $PID)..."
        kill "$PID"
        sleep 2
        if kill -0 "$PID" 2>/dev/null; then
            echo "Force killing..."
            kill -9 "$PID"
        fi
        echo "Backend stopped"
    else
        echo "Backend not running (stale PID file)"
    fi
    rm "$PID_FILE"
else
    echo "No backend PID file found. Checking for orphan processes..."
    pkill -f "python -m app.mcp_native.*8100" && echo "Killed orphan MCP process" || echo "No MCP process found"
fi

if [ -f "$WORKER_PID_FILE" ]; then
    WORKER_PID=$(cat "$WORKER_PID_FILE")
    if kill -0 "$WORKER_PID" 2>/dev/null; then
        echo "Stopping worker (PID: $WORKER_PID)..."
        kill "$WORKER_PID"
        sleep 2
        if kill -0 "$WORKER_PID" 2>/dev/null; then
            echo "Force killing worker..."
            kill -9 "$WORKER_PID"
        fi
        echo "Worker stopped"
    else
        echo "Worker not running (stale PID file)"
    fi
    rm "$WORKER_PID_FILE"
else
    echo "No worker PID file found. Checking for orphan processes..."
    pkill -f "dramatiq app.workers.agent_runner" && echo "Killed orphan worker process" || echo "No worker process found"
fi
