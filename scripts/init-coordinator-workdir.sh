#!/usr/bin/env bash
# Create a standalone coordinator workspace OUTSIDE the Control Tower repo.
#
# Usage: init-coordinator-workdir.sh [dir] [ttl_seconds]
#   dir          workspace to create (default: ~/ct-coordinator)
#   ttl_seconds  coordinator token lifetime (default: 28800 = one working day)
#
# The workspace gets:
#   - CLAUDE.md / AGENTS.md / PROJECT.md generated from docs/coordinator-rules.md
#     (claude / codex / agy each auto-load their own file)
#   - .agents/mcp_config.json for agy (serverUrl schema, fresh token)
#   - a ready-to-paste `claude mcp add` command for Claude Code
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
WORKDIR="${1:-$HOME/ct-coordinator}"
TTL="${2:-28800}"
NATIVE_URL="${MCP_NATIVE_URL:-http://localhost:8100/mcp}"

if [[ -z "${MCP_TOKEN_SECRET:-}" && -f "$PROJECT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

mkdir -p "$WORKDIR/.agents"

# One source of truth for the instruction text, three CLI-specific filenames.
for name in CLAUDE.md AGENTS.md PROJECT.md; do
  cp "$PROJECT_DIR/docs/coordinator-rules.md" "$WORKDIR/$name"
done

TOKEN=$("$SCRIPT_DIR/issue-coordinator-token.sh" coordinator "" "$TTL" | tail -1)

cat > "$WORKDIR/.agents/mcp_config.json" <<JSON
{
  "mcpServers": {
    "control-tower": {
      "serverUrl": "$NATIVE_URL",
      "headers": {
        "Authorization": "Bearer $TOKEN"
      }
    }
  }
}
JSON
chmod 600 "$WORKDIR/.agents/mcp_config.json"

echo "Coordinator workspace ready: $WORKDIR"
echo
echo "  agy:    cd $WORKDIR && agy          (reads PROJECT.md + .agents/mcp_config.json)"
echo "  codex:  cd $WORKDIR && codex        (reads AGENTS.md; MCP via -c flags or config.toml)"
echo "  claude: cd $WORKDIR && claude mcp add --transport http control-tower $NATIVE_URL \\"
echo "            --header \"Authorization: Bearer $TOKEN\""
echo
echo "Token TTL: ${TTL}s — re-run this script to refresh the token in place."
