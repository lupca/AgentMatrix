#!/usr/bin/env bash
# Usage: issue-coordinator-token.sh [role] [task_id] [ttl_seconds]
#   role        coordinator (default) | executor
#   task_id     required scope for executor tokens
#   ttl_seconds token lifetime; e.g. 28800 for an interactive working day
set -euo pipefail

role="${1:-coordinator}"
task_id="${2:-}"
ttl="${3:-}"
if [[ -z "${MCP_TOKEN_SECRET:-}" ]]; then
  echo "MCP_TOKEN_SECRET is required" >&2
  exit 1
fi

cd "$(dirname "$0")/../backend"

# Use the same interpreter the backend runs with — system python3 lacks fastmcp.
if [[ -x "venv/bin/python" ]]; then
  PYTHON="venv/bin/python"
elif [[ -x "../.venv/bin/python" ]]; then
  PYTHON="../.venv/bin/python"
else
  PYTHON="python3"
fi

args=(--role "$role")
if [[ -n "$task_id" ]]; then
  args+=(--task-id "$task_id")
fi
if [[ -n "$ttl" ]]; then
  args+=(--ttl "$ttl")
fi
PYTHONPATH=. "$PYTHON" -m app.mcp_native_issue_token "${args[@]}"
