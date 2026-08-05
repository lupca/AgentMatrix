"""Dispatch coordinator turns to account-backed CLI clients.

The coordinator deliberately does not pass a session identifier to a CLI.  A
new process is started for every turn and the durable session history is
included in its prompt by :func:`format_history_as_prompt`.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from app.services.process_manager import ProcessManager, ProcessResult, ProcessStatus
from app.core.config import settings
from app.services.cli_command import SUPPORTED_CLIS, build_cli_command


INSTRUCTION_FILES = {"claude": "CLAUDE.md", "codex": "AGENTS.md", "agy": "PROJECT.md", "qwen": "CLAUDE.md"}
COORDINATOR_RULES_PATH = "docs/coordinator-rules.md"


@dataclass(frozen=True)
class CLIRoute:
    """The CLI and provider associated with a coordinator model."""

    cli: str
    provider: str


class CLIDispatchError(RuntimeError):
    """Raised when a coordinator CLI exits unsuccessfully."""

    def __init__(self, result: ProcessResult):
        self.result = result
        super().__init__(result.error or result.status.value)

    @property
    def retryable(self) -> bool:
        return self.result.status == ProcessStatus.TIMEOUT


def route_model(model: str, provider: str | None = None) -> CLIRoute:
    """Resolve a model to its account-backed CLI.

    Claude and Gemini are the supported subscription-backed coordinator
    providers.  Codex is also supported by the dispatcher so callers can use
    the same process lifecycle for a Codex model when one is configured.
    """

    normalized_model = (model or "").strip().lower()
    normalized_provider = provider.strip().lower() if provider else None

    if "claude" in normalized_model or (
        not normalized_model and normalized_provider == "anthropic"
    ):
        route = CLIRoute("claude", "anthropic")
    elif "gemini" in normalized_model or (
        not normalized_model and normalized_provider == "google"
    ):
        route = CLIRoute("agy", "google")
    elif (
        normalized_model.startswith(("gpt-", "o1-", "chatgpt-"))
        or "codex" in normalized_model
        or (not normalized_model and normalized_provider in {"openai", "codex"})
    ):
        route = CLIRoute("codex", "openai")
    elif "qwen" in normalized_model or (
        not normalized_model and normalized_provider == "alibaba"
    ):
        route = CLIRoute("qwen", "alibaba")
    else:
        raise ValueError(
            f"Cannot infer CLI for coordinator model '{model}'. "
            "Use a claude, gemini, codex, or qwen model."
        )

    if normalized_provider and normalized_provider not in {
        route.provider,
        route.cli,
        "codex" if route.cli == "codex" else "",
    }:
        raise ValueError(
            f"Model '{model}' belongs to provider '{route.provider}', "
            f"not '{normalized_provider}'."
        )
    return route


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def format_history_as_prompt(messages: list[dict[str, Any]]) -> str:
    """Format canonical session messages as a readable CLI prompt.

    System messages are emitted first as a preamble.  Tool messages and
    assistant tool-call metadata are retained as explicit turns instead of
    being silently discarded, which keeps replayed history understandable to
    a CLI model.
    """

    sections: list[str] = []
    for message in messages:
        role = str(message.get("role", "user")).lower()
        if role == "system":
            label = "SYSTEM"
        elif role == "assistant":
            label = "ASSISTANT"
        elif role == "tool":
            tool_name = message.get("name") or message.get("tool_name")
            label = f"TOOL ({tool_name})" if tool_name else "TOOL"
        elif role == "user":
            label = "USER"
        else:
            label = role.upper()

        body = _content_text(message.get("content", ""))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            body = f"{body}\nTOOL_CALLS:\n{_content_text(tool_calls)}" if body else (
                f"TOOL_CALLS:\n{_content_text(tool_calls)}"
            )
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            body = f"TOOL_CALL_ID: {tool_call_id}\n{body}"
        sections.append(f"{label}:\n{body}" if body else f"{label}:")

    return "\n\n".join(sections)


def build_mcp_config(
    api_url: str, token: str, *, native_url: str | None = None,
    role: str = "coordinator",
) -> dict[str, Any]:
    """Build the native streamable-HTTP MCP config for any supported CLI."""

    del api_url  # retained in the signature for callers migrating from GĐ2
    return {"mcpServers": {"agmx": {
        "type": "http",
        "url": native_url or settings.MCP_NATIVE_URL,
        "headers": {"Authorization": f"Bearer {token}", "X-CT-Role": role},
    }}}


def build_instruction_text(source: str) -> str:
    """Create the compact CLI instruction payload from one canonical source."""

    summary = (
        "AGMX coordinator: use MCP tools only; follow task state "
        "todo→dispatched→awaiting-review→in-review→done; obey four-eyes; "
        "read and follow `next` in every tool result.\n\n"
    )
    body = source.strip()
    text = summary + body
    if len(text) > 2048:
        text = text[:2048].rsplit("\n", 1)[0].rstrip() + "\n"
    return text


def write_instruction_files(workspace: str, source_path: str | None = None) -> dict[str, str]:
    """Write Claude/Codex/agy instruction files from one source document.

    Existing files are replaced intentionally: these are generated projections
    and must not drift. ``GEMINI.md`` is never generated.
    """

    root = os.path.abspath(workspace)
    canonical = source_path or os.path.join(root, COORDINATOR_RULES_PATH)
    if not os.path.exists(canonical):
        canonical = os.path.join(root, "README.md")
    with open(canonical, encoding="utf-8") as fh:
        content = build_instruction_text(fh.read())
    written: dict[str, str] = {}
    for cli, filename in INSTRUCTION_FILES.items():
        path = os.path.join(root, filename)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        written[cli] = path
    return written


def write_mcp_config(
    api_url: str, token: str, *, native_url: str | None = None, role: str = "coordinator"
) -> str:
    """Write a one-shot MCP config file for a single CLI spawn.

    Every coordinator chat turn starts a fresh CLI process, so the config is
    regenerated per spawn rather than cached.
    """

    fd, path = tempfile.mkstemp(prefix="ct-mcp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(build_mcp_config(api_url, token, native_url=native_url, role=role), fh)
    except BaseException:
        os.unlink(path)
        raise
    return path


def write_coordinator_instruction_files(workspace: str) -> dict[str, str]:
    """Generate CLAUDE.md, AGENTS.md and PROJECT.md from canonical rules."""

    source = os.path.join(workspace, COORDINATOR_RULES_PATH)
    return write_instruction_files(workspace, source)


class CLIDispatcher:
    """Run one account-backed CLI process and expose its output asynchronously."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 14_400,
        working_directory: str | None = None,
        process_manager_factory: Callable[..., ProcessManager] | None = None,
        api_url: str | None = None,
        mcp_token: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.timeout_seconds = timeout_seconds
        self.working_directory = working_directory or os.getcwd()
        self.process_manager_factory = process_manager_factory
        self.api_url = api_url  # deprecated compatibility argument
        self.mcp_secret = mcp_token or os.environ.get("MCP_TOKEN_SECRET", "")

    def _new_process_manager(self) -> ProcessManager:
        factory = self.process_manager_factory or ProcessManager
        return factory(timeout_seconds=self.timeout_seconds)

    async def spawn(
        self,
        cli: str,
        model: str,
        prompt: str,
        effort: str | None = None,
        cwd: str | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> AsyncIterator[str]:
        """Spawn a CLI and yield stdout chunks until it exits.

        ``ProcessManager`` is intentionally synchronous because it is shared
        with the worker process.  Its generator runs in a thread while this
        async generator forwards each output item to the event loop.

        ``on_start``, if given, is invoked with the child PID once the
        process has been spawned -- routed through the same
        thread-safe ``publish`` used for output/result so the callback runs
        back on the event-loop thread (``ProcessManager.on_start`` itself
        fires from the ``run_process`` background thread, and callers such as
        ``spec_plan_generator`` use this to persist the PID on a SQLAlchemy
        session that must not be touched from a second thread).
        """

        effective_cwd = cwd or self.working_directory
        base_command = build_cli_command(
            cli, model, prompt, effort=effort, timeout_seconds=self.timeout_seconds
        )
        from app.services.mcp_attach import attach_mcp

        command, extra_env, cleanup_paths = attach_mcp(
            cli=cli,
            command=base_command,
            workdir=effective_cwd,
            role="coordinator",
            timeout_seconds=3600,
            mcp_secret=self.mcp_secret,
        )
        if (cli or "").strip().lower() == "qwen":
            extra_env["QWEN_CODE_SUPPRESS_YOLO_WARNING"] = "1"
        process_manager = self._new_process_manager()
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[tuple[str, object]] = asyncio.Queue()

        def publish(kind: str, value: object) -> None:
            loop.call_soon_threadsafe(events.put_nowait, (kind, value))

        process_manager.on_start = lambda pid: publish("pid", pid)

        def run_process() -> None:
            try:
                stream: Iterable[str | ProcessResult] = process_manager.run_with_streaming(
                    command,
                    effective_cwd,
                    env=extra_env,
                )
                for item in stream:
                    if isinstance(item, ProcessResult):
                        publish("result", item)
                    else:
                        publish("output", str(item))
            except BaseException as exc:
                publish("error", exc)
            finally:
                publish("done", None)

        worker = asyncio.create_task(asyncio.to_thread(run_process))
        try:
            while True:
                kind, value = await events.get()
                if kind == "output":
                    output = str(value)
                    yield output if output.endswith(("\n", "\r")) else f"{output}\n"
                elif kind == "pid":
                    if on_start is not None:
                        on_start(int(value))
                elif kind == "result":
                    result = value
                    if isinstance(result, ProcessResult) and result.status != ProcessStatus.COMPLETED:
                        raise CLIDispatchError(result)
                elif kind == "error":
                    raise value  # type: ignore[misc]
                elif kind == "done":
                    break
        except asyncio.CancelledError:
            cancel = getattr(process_manager, "cancel", None)
            if callable(cancel):
                cancel()
            raise
        finally:
            terminate = getattr(process_manager, "terminate", None)
            if callable(terminate):
                terminate()
            if not worker.done():
                worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
            from app.services.mcp_attach import detach_mcp

            detach_mcp(cleanup_paths)


# A concise alias used by callers that prefer the verb used in the task spec.
format_prompt = format_history_as_prompt


__all__ = [
    "CLIDispatchError",
    "CLIDispatcher",
    "CLIRoute",
    "SUPPORTED_CLIS",
    "build_cli_command",
    "build_mcp_config",
    "format_history_as_prompt",
    "format_prompt",
    "route_model",
    "write_mcp_config",
]
