#!/usr/bin/env bash
# Create a standalone coordinator workspace OUTSIDE the Control Tower repo.
#
# Usage: init-coordinator-workdir.sh [dir] [ttl_seconds] [--with name=url]...
#   dir           workspace to create (default: ~/ct-coordinator)
#   ttl_seconds   coordinator token lifetime (default: 28800 = one working day)
#   --with n=url  add an extra HTTP MCP server to the workspace config
#                 (repeatable), e.g. --with code-review-graph=http://localhost:9200/mcp
#
# The workspace gets:
#   - CLAUDE.md / AGENTS.md / PROJECT.md generated from docs/coordinator-rules.md
#     (claude / codex / agy each auto-load their own file)
#   - .agents/mcp_config.json for agy: control-tower + every --with server
#     (serverUrl schema, fresh token for control-tower)
#   - ready-to-paste `claude mcp add` commands for Claude Code
#
# Re-run any time to refresh the token in place; --with list replaces the
# extra servers on each run (config is regenerated, not merged).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
NATIVE_URL="${MCP_NATIVE_URL:-http://localhost:8100/mcp}"

WORKDIR=""
TTL="28800"
EXTRA_NAMES=()
EXTRA_URLS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with)
      shift
      entry="${1:?--with requires name=url}"
      EXTRA_NAMES+=("${entry%%=*}")
      EXTRA_URLS+=("${entry#*=}")
      ;;
    *)
      if [[ -z "$WORKDIR" ]]; then
        WORKDIR="$1"
      else
        TTL="$1"
      fi
      ;;
  esac
  shift
done
WORKDIR="${WORKDIR:-$HOME/ct-coordinator}"

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

# Build the agy workspace config: control-tower plus every --with server.
{
  echo '{'
  echo '  "mcpServers": {'
  echo '    "control-tower": {'
  echo "      \"serverUrl\": \"$NATIVE_URL\","
  echo '      "headers": {'
  echo "        \"Authorization\": \"Bearer $TOKEN\""
  echo '      }'
  printf '    }'
  for i in "${!EXTRA_NAMES[@]}"; do
    printf ',\n    "%s": {\n      "serverUrl": "%s"\n    }' "${EXTRA_NAMES[$i]}" "${EXTRA_URLS[$i]}"
  done
  echo
  echo '  }'
  echo '}'
} > "$WORKDIR/.agents/mcp_config.json"
chmod 600 "$WORKDIR/.agents/mcp_config.json"

# Claude Code project-scoped config: shows up under "Project MCPs" the moment
# a claude session starts in the workspace (approve it on first run).
{
  echo '{'
  echo '  "mcpServers": {'
  echo '    "control-tower": {'
  echo '      "type": "http",'
  echo "      \"url\": \"$NATIVE_URL\","
  echo '      "headers": {'
  echo "        \"Authorization\": \"Bearer $TOKEN\""
  echo '      }'
  printf '    }'
  for i in "${!EXTRA_NAMES[@]}"; do
    printf ',\n    "%s": {\n      "type": "http",\n      "url": "%s"\n    }' "${EXTRA_NAMES[$i]}" "${EXTRA_URLS[$i]}"
  done
  echo
  echo '  }'
  echo '}'
} > "$WORKDIR/.mcp.json"
chmod 600 "$WORKDIR/.mcp.json"

# Codex workspace config: token stays out of the file via bearer_token_env_var.
mkdir -p "$WORKDIR/.codex"
{
  echo '[mcp_servers.control-tower]'
  echo "url = \"$NATIVE_URL\""
  echo 'bearer_token_env_var = "CT_MCP_TOKEN"'
  for i in "${!EXTRA_NAMES[@]}"; do
    echo
    echo "[mcp_servers.${EXTRA_NAMES[$i]}]"
    echo "url = \"${EXTRA_URLS[$i]}\""
  done
} > "$WORKDIR/.codex/config.toml"
echo "export CT_MCP_TOKEN=$TOKEN" > "$WORKDIR/.codex/env.sh"
chmod 600 "$WORKDIR/.codex/config.toml" "$WORKDIR/.codex/env.sh"

echo "Coordinator workspace ready: $WORKDIR"
if [[ ${#EXTRA_NAMES[@]} -gt 0 ]]; then
  echo "Extra MCP servers: ${EXTRA_NAMES[*]}"
fi
echo
echo "  claude: cd $WORKDIR && claude       (.mcp.json — approve 'Project MCPs' on first run)"
echo "  agy:    cd $WORKDIR && agy           (PROJECT.md + .agents/mcp_config.json)"
echo "  codex:  cd $WORKDIR && source .codex/env.sh && codex   (AGENTS.md + .codex/config.toml)"
echo
echo "Token TTL: ${TTL}s — re-run this script to refresh the token in place."
