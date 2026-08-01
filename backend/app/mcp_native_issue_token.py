"""Small CLI for issuing native MCP role tokens."""

from __future__ import annotations

import argparse
import os

from app.mcp_native import issue_token


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("coordinator", "executor"), default="coordinator")
    parser.add_argument("--task-id")
    parser.add_argument(
        "--ttl",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Token lifetime in seconds (default: 1h coordinator, 15m "
            "executor). Use a longer value for interactive coordinator "
            "sessions, e.g. 28800 for a working day."
        ),
    )
    args = parser.parse_args()
    secret = os.environ.get("MCP_TOKEN_SECRET", "")
    if not secret:
        raise SystemExit("MCP_TOKEN_SECRET is required")
    print(issue_token(secret, role=args.role, task_id=args.task_id, ttl_seconds=args.ttl))


if __name__ == "__main__":
    main()
