"""Small CLI for issuing native MCP role tokens."""

from __future__ import annotations

import argparse
import os

from app.mcp_native import issue_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("coordinator", "executor"), default="coordinator")
    parser.add_argument("--task-id")
    args = parser.parse_args()
    secret = os.environ.get("MCP_TOKEN_SECRET", "")
    if not secret:
        raise SystemExit("MCP_TOKEN_SECRET is required")
    print(issue_token(secret, role=args.role, task_id=args.task_id))


if __name__ == "__main__":
    main()
