#!/bin/bash
# Stop Control Tower V2 Backend

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PID_FILE="$PROJECT_DIR/.backend.pid"

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
    echo "No PID file found. Checking for orphan processes..."
    pkill -f "uvicorn app.main:app.*8001" && echo "Killed orphan process" || echo "No backend process found"
fi
