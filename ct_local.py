#!/usr/bin/env python3
"""Direct service caller for Control Tower (bypasses MCP HTTP)."""
import json
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from app.db.session import SessionLocal
from app.services.command_router import CommandRouter


def call_tool(tool_name: str, args: dict):
    import asyncio
    db = SessionLocal()
    try:
        router = CommandRouter(db)
        result = asyncio.run(router.execute_tool(tool_name, args, session_id="cli-local"))
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: ct_local.py <tool_name> '<json_args>'")
        sys.exit(1)
    tool = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    call_tool(tool, args)
