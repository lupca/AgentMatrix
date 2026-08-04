"""Canonical command construction for every supported CLI agent."""

from __future__ import annotations

import shlex


SUPPORTED_CLIS = {"agy", "claude", "codex", "qwen"}
_EFFORT_SUFFIXES = ("-low", "-medium", "-high", "-extra-high", "-max", "-ultra")
_AGY_PRINT_TIMEOUT_FALLBACK_SECONDS = 1_800


def _model_carries_effort(model: str | None) -> bool:
    return (model or "").strip().lower().endswith(_EFFORT_SUFFIXES)


def _agy_print_timeout(timeout_seconds: int | None) -> str:
    seconds = (
        timeout_seconds
        if timeout_seconds is not None and timeout_seconds > 0
        else _AGY_PRINT_TIMEOUT_FALLBACK_SECONDS
    )
    return f"{seconds}s"


def build_cli_command(
    cli: str,
    model: str | None,
    prompt: str,
    mcp_config_path: str | None = None,
    effort: str | None = None,
    timeout_seconds: int | None = None,
) -> str:
    """Build the one canonical argv shape used by planner, critic, dispatch and review.

    The returned shell command is only a transport format for the existing
    process manager; ``shlex.join`` preserves prompt boundaries.  CLI-native
    output and permission flags live here so no caller can silently drift.
    """
    normalized_cli = (cli or "").strip().lower()
    if normalized_cli not in SUPPORTED_CLIS:
        raise ValueError(
            f"Unsupported CLI '{cli}'. Supported CLIs: {', '.join(sorted(SUPPORTED_CLIS))}"
        )

    normalized_effort = (effort or "").strip().lower() or None
    if normalized_effort and _model_carries_effort(model):
        normalized_effort = None

    if normalized_cli == "claude":
        argv = ["claude"]
        if model:
            argv += ["--model", model]
        if normalized_effort:
            argv += ["--effort", normalized_effort]
        if mcp_config_path:
            argv += ["--mcp-config", mcp_config_path]
        argv += [
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "-p",
            prompt,
        ]
    elif normalized_cli == "agy":
        argv = ["agy"]
        if model:
            argv += ["--model", model]
        if normalized_effort:
            argv += ["--effort", normalized_effort]
        # agy requires the prompt immediately after --print. Its timeout is
        # a real CLI limit and therefore belongs after the prompt.
        argv += [
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--print",
            prompt,
            "--print-timeout",
            _agy_print_timeout(timeout_seconds),
        ]
    elif normalized_cli == "qwen":
        argv = ["qwen"]
        if model:
            argv += ["-m", model]
        if mcp_config_path:
            argv += ["--mcp-config", mcp_config_path]
        argv += ["--yolo", "-p", prompt, "--output-format", "stream-json"]
    else:
        argv = ["codex", "exec", "--json"]
        if model:
            argv += ["-m", model]
        if normalized_effort:
            argv += ["-c", f"model_reasoning_effort={normalized_effort}"]
        # codex exec does not support --mcp-config; mcp_attach injects its
        # native config through -c and CT_MCP_TOKEN instead.
        argv += ["--dangerously-bypass-approvals-and-sandbox", prompt]

    return shlex.join(argv)


__all__ = ["SUPPORTED_CLIS", "build_cli_command"]
