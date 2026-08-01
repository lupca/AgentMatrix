#!/usr/bin/env bash
set -euo pipefail

role="${1:-coordinator}"
task_id="${2:-}"
if [[ -z "${MCP_TOKEN_SECRET:-}" ]]; then
  echo "MCP_TOKEN_SECRET is required" >&2
  exit 1
fi

cd "$(dirname "$0")/../backend"
if [[ -n "$task_id" ]]; then
  PYTHONPATH=. python3 -m app.mcp_native_issue_token --role "$role" --task-id "$task_id"
else
  PYTHONPATH=. python3 -m app.mcp_native_issue_token --role "$role"
fi
