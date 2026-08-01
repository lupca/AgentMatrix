"""Spawn-time MCP attachment per CLI adapter."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from typing import Optional

from app.core.config import settings
from app.mcp_native import issue_token
from app.services.cli_dispatcher import build_mcp_config

_MCP_GRACE_PERIOD_SECONDS = 120

# Suffix for the backup taken when attach has to modify a pre-existing file
# (agy's fixed-path config). detach_mcp restores it instead of deleting.
_BACKUP_SUFFIX = ".ct-orig"


def _ensure_git_exclude(workdir: str, entry: str = ".agents") -> None:
    """Hide ``entry`` from git in ``workdir``, including linked worktrees.

    Git only honours ``info/exclude`` in the COMMON git dir — writing to a
    linked worktree's private ``.git/worktrees/<name>/info/exclude`` is
    silently ignored — so the path must come from git itself.
    """

    if not os.path.exists(os.path.join(workdir, ".git")):
        return
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return
        exclude_file = proc.stdout.strip()
        if not os.path.isabs(exclude_file):
            exclude_file = os.path.abspath(os.path.join(workdir, exclude_file))
        os.makedirs(os.path.dirname(exclude_file), exist_ok=True)
        existing = ""
        if os.path.exists(exclude_file):
            with open(exclude_file, "r", encoding="utf-8") as f:
                existing = f.read()
        if entry not in existing.splitlines():
            with open(exclude_file, "a", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(f"{entry}\n")
    except (OSError, subprocess.SubprocessError):
        pass


def detach_mcp(cleanup_paths: list[str] | None) -> None:
    """Undo :func:`attach_mcp`'s filesystem side effects.

    A path with a ``.ct-orig`` backup next to it is restored to its original
    content (the file pre-existed and was modified in place); anything else
    is deleted.
    """

    for path in cleanup_paths or []:
        backup = path + _BACKUP_SUFFIX
        try:
            if os.path.exists(backup):
                os.replace(backup, path)
            elif os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def attach_mcp(
    cli: str,
    command: str,
    workdir: str,
    *,
    task_id: Optional[str] = None,
    role: str = "executor",
    timeout_seconds: Optional[int] = None,
    mcp_secret: Optional[str] = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Attach native MCP server config to a CLI command at spawn-time.

    ``task_id`` scopes an executor token to its task and must be None for
    coordinator tokens. Returns (final_command_str, extra_env_dict,
    cleanup_file_paths) — pass the paths to :func:`detach_mcp` when the CLI
    exits.
    """
    secret = mcp_secret or settings.MCP_TOKEN_SECRET
    if not secret:
        raise RuntimeError("MCP_TOKEN_SECRET is required for native MCP")

    normalized_cli = cli.strip().lower()
    ttl = (timeout_seconds or settings.RUN_TIMEOUT_SECONDS) + _MCP_GRACE_PERIOD_SECONDS
    token = issue_token(secret, role=role, task_id=task_id, ttl_seconds=ttl)

    extra_env: dict[str, str] = {}
    cleanup_paths: list[str] = []
    final_command = command

    if normalized_cli == "claude":
        mcp_payload = build_mcp_config(
            settings.MCP_NATIVE_URL,
            token,
            native_url=settings.MCP_NATIVE_URL,
            role=role,
        )
        handle = tempfile.NamedTemporaryFile(
            prefix="ct-mcp-", suffix=".json", mode="w", encoding="utf-8", delete=False
        )
        try:
            with handle:
                json.dump(mcp_payload, handle)
            os.chmod(handle.name, 0o600)
            cleanup_paths.append(handle.name)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

        argv = shlex.split(command)
        if "-p" in argv:
            idx = argv.index("-p")
            argv = argv[:idx] + ["--mcp-config", handle.name] + argv[idx:]
        else:
            if len(argv) > 1:
                argv = argv[:-1] + ["--mcp-config", handle.name] + [argv[-1]]
            else:
                argv.extend(["--mcp-config", handle.name])
        final_command = shlex.join(argv)

    elif normalized_cli == "codex":
        extra_env["CT_MCP_TOKEN"] = token
        argv = shlex.split(command)
        mcp_flags = [
            "-c",
            f"mcp_servers.control-tower.url={settings.MCP_NATIVE_URL}",
            "-c",
            "mcp_servers.control-tower.bearer_token_env_var=CT_MCP_TOKEN",
        ]
        if len(argv) >= 2 and argv[0] == "codex" and argv[1] == "exec":
            argv = argv[:2] + mcp_flags + argv[2:]
        else:
            argv = argv[:1] + mcp_flags + argv[1:]
        final_command = shlex.join(argv)

    elif normalized_cli == "agy":
        mcp_dir = os.path.join(workdir, ".agents")
        os.makedirs(mcp_dir, exist_ok=True)
        mcp_file = os.path.join(mcp_dir, "mcp_config.json")
        # agy reads this fixed path, so a user's own config may already live
        # here (coordinator workdir is a real directory, not a worktree).
        # Merge our server in and keep a backup so detach_mcp can restore
        # the original instead of destroying it.
        mcp_payload: dict = {"mcpServers": {}}
        if os.path.exists(mcp_file):
            original_text: Optional[str] = None
            try:
                with open(mcp_file, "r", encoding="utf-8") as f:
                    original_text = f.read()
            except OSError:
                pass
            if original_text is not None:
                # Back up even unparseable content — detach restores it verbatim.
                backup = mcp_file + _BACKUP_SUFFIX
                with open(backup, "w", encoding="utf-8") as f:
                    f.write(original_text)
                os.chmod(backup, 0o600)
                try:
                    existing = json.loads(original_text)
                    if isinstance(existing, dict) and isinstance(
                        existing.get("mcpServers"), dict
                    ):
                        mcp_payload = existing
                except json.JSONDecodeError:
                    pass
        mcp_payload.setdefault("mcpServers", {})["control-tower"] = {
            "serverUrl": settings.MCP_NATIVE_URL,
            "headers": {
                "Authorization": f"Bearer {token}",
            },
        }
        with open(mcp_file, "w", encoding="utf-8") as f:
            json.dump(mcp_payload, f, indent=2)
        os.chmod(mcp_file, 0o600)
        cleanup_paths.append(mcp_file)
        _ensure_git_exclude(workdir)

    return final_command, extra_env, cleanup_paths
