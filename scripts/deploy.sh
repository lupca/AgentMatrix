#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$ROOT_DIR"

echo "=== Control Tower V2 Deployment ==="

if [ ! -f .env ]; then
    echo "Creating .env from .env.example..."
    cp .env.example .env
fi

# Export environment variables from .env
set -a
source .env
set +a

echo "Building and starting Docker Compose containers..."
if command -v docker-compose &> /dev/null; then
    COMPOSE_CMD="docker-compose"
else
    COMPOSE_CMD="docker compose"
fi

$COMPOSE_CMD up --build -d

echo "Waiting for services to become healthy..."
sleep 5

$COMPOSE_CMD ps

BACKEND_HOST_PORT="${BACKEND_PORT:-8000}"
CHAT_HOST_PORT="${CHAT_PORT:-8080}"
DASHBOARD_HOST_PORT="${DASHBOARD_PORT:-8501}"
DB_HOST_PORT="${POSTGRES_PORT:-5432}"

echo ""
echo "=== Deployment Finished ==="
echo "Backend API:      http://localhost:${BACKEND_HOST_PORT}"
echo "Chat UI:          http://localhost:${CHAT_HOST_PORT}"
echo "Dashboard:        http://localhost:${DASHBOARD_HOST_PORT}"
echo "Database:         localhost:${DB_HOST_PORT}"
