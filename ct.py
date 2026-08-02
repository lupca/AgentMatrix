#!/usr/bin/env python3
"""Quick MCP tool caller for Control Tower."""
import json
import sys
import httpx

MCP_URL = "http://localhost:8100/mcp"
TOKEN_PATH = "/home/lupca/^Coject-mangment/.mcp.json"
SESSION_FILE = "/tmp/ct_mcp_session_id"


def get_token():
    try:
        with open(TOKEN_PATH) as f:
            cfg = json.load(f)
        return cfg["mcpServers"]["control-tower"]["headers"]["Authorization"].replace("Bearer ", "")
    except Exception:
        return None


def base_headers(token: str | None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def initialize_session(token: str | None) -> str:
    """Initialize MCP session and return session ID."""
    headers = base_headers(token)
    payload = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "ct.py", "version": "0.1.0"},
        },
    }
    resp = httpx.post(MCP_URL, json=payload, headers=headers, timeout=30)
    session_id = resp.headers.get("Mcp-Session-Id")
    if not session_id:
        raise RuntimeError(f"No session ID in response headers: {resp.headers}")
    with open(SESSION_FILE, "w") as f:
        f.write(session_id)
    return session_id


def get_session_id(token: str | None) -> str:
    try:
        with open(SESSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return initialize_session(token)


def call_tool(tool_name: str, args: dict):
    token = get_token()
    session_id = get_session_id(token)
    headers = base_headers(token)
    headers["Mcp-Session-Id"] = session_id

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }

    resp = httpx.post(MCP_URL, json=payload, headers=headers, timeout=120)
    result = resp.json()
    if "error" in result:
        # Session expired? Re-init and retry once
        if "Session not found" in str(result.get("error", {}).get("message", "")):
            session_id = initialize_session(token)
            headers["Mcp-Session-Id"] = session_id
            resp = httpx.post(MCP_URL, json=payload, headers=headers, timeout=120)
            result = resp.json()
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result.get("result", result), indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ct.py <tool_name> '<json_args>'")
        sys.exit(1)
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    call_tool(tool, args)
