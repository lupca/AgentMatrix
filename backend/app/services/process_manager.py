"""Subprocess lifecycle management for background agent executions."""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Generator, Optional

import psutil

logger = logging.getLogger(__name__)


class ProcessStatus(Enum):
    RUNNING = "running"
    COMPLETED = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProcessResult:
    status: ProcessStatus
    exit_code: Optional[int]
    error: Optional[str]


_EOF = object()


class ProcessManager:
    """Run one subprocess and reliably clean up its entire process group."""

    def __init__(
        self,
        timeout_seconds: int = 14_400,
        *,
        terminate_grace_seconds: float = 5.0,
        poll_interval: float = 0.1,
        cancel_check: Optional[Callable[[], bool]] = None,
        on_start: Optional[Callable[[int], None]] = None,
        on_heartbeat: Optional[Callable[[int], None]] = None,
        heartbeat_interval: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self.timeout = timeout_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self.poll_interval = poll_interval
        self.cancel_check = cancel_check
        self.on_start = on_start
        self.on_heartbeat = on_heartbeat
        self.heartbeat_interval = heartbeat_interval
        self.process: Optional[subprocess.Popen[str]] = None
        self._cancelled = threading.Event()
        self._terminate_lock = threading.Lock()
        self._termination_complete = False

    def run_with_streaming(
        self,
        command: str,
        cwd: str,
        env: Optional[dict[str, str]] = None,
    ) -> Generator[str | ProcessResult, None, None]:
        """Yield merged stdout/stderr lines followed by exactly one result."""
        process_env = os.environ.copy()
        if env:
            process_env.update({key: str(value) for key, value in env.items()})

        workdir = Path(cwd)
        if not workdir.is_dir():
            yield ProcessResult(
                ProcessStatus.FAILED,
                -1,
                f"Working directory does not exist: {cwd}",
            )
            return

        output_queue: queue.Queue[object] = queue.Queue()
        started_at = time.monotonic()
        last_heartbeat_at = started_at
        result: Optional[ProcessResult] = None

        try:
            self.process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=process_env,
                start_new_session=True,
            )
            if self.on_start is not None:
                self.on_start(self.process.pid)

            reader = threading.Thread(
                target=self._read_output,
                args=(output_queue,),
                name=f"agent-output-{self.process.pid}",
                daemon=True,
            )
            reader.start()

            while result is None:
                if self._is_cancelled():
                    self._terminate()
                    result = ProcessResult(
                        ProcessStatus.CANCELLED,
                        -1,
                        "Cancelled by user",
                    )
                    break

                now = time.monotonic()
                if (
                    self.on_heartbeat is not None
                    and self.process is not None
                    and self.process.poll() is None
                    and now - last_heartbeat_at >= self.heartbeat_interval
                ):
                    try:
                        self.on_heartbeat(self.process.pid)
                    except Exception:
                        logger.warning("on_heartbeat callback failed", exc_info=True)
                    last_heartbeat_at = now

                elapsed = now - started_at
                if elapsed >= self.timeout:
                    self._terminate()
                    result = ProcessResult(
                        ProcessStatus.TIMEOUT,
                        -1,
                        f"Timeout after {self.timeout}s",
                    )
                    break

                try:
                    item = output_queue.get(
                        timeout=min(self.poll_interval, max(self.timeout - elapsed, 0.001))
                    )
                except queue.Empty:
                    if self.process.poll() is not None and not reader.is_alive():
                        break
                    continue

                if item is _EOF:
                    if self._cancelled.is_set():
                        result = ProcessResult(
                            ProcessStatus.CANCELLED,
                            -1,
                            "Cancelled by user",
                        )
                    break
                yield str(item)

            if result is None:
                # Drain lines queued just before EOF so the final output is not lost.
                while True:
                    try:
                        item = output_queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not _EOF:
                        yield str(item)

                exit_code = self.process.wait()
                if exit_code == 0:
                    result = ProcessResult(ProcessStatus.COMPLETED, exit_code, None)
                else:
                    result = ProcessResult(
                        ProcessStatus.FAILED,
                        exit_code,
                        f"Exit code: {exit_code}",
                    )

            yield result
        except GeneratorExit:
            self._terminate()
            raise
        except Exception as exc:
            self._terminate()
            yield ProcessResult(ProcessStatus.FAILED, -1, str(exc))
        finally:
            if self.process and self.process.poll() is None:
                self._terminate()

    def _read_output(self, output_queue: queue.Queue[object]) -> None:
        try:
            if self.process is None or self.process.stdout is None:
                return
            for line in self.process.stdout:
                output_queue.put(line.rstrip("\r\n"))
        finally:
            output_queue.put(_EOF)

    def _is_cancelled(self) -> bool:
        if self._cancelled.is_set():
            return True
        if self.cancel_check is None:
            return False
        try:
            if self.cancel_check():
                self._cancelled.set()
                return True
        except Exception:
            # A transient control-channel failure must not kill the agent process.
            return False
        return False

    def cancel(self) -> None:
        """Request cancellation and immediately signal a running process."""
        self._cancelled.set()
        self._terminate()

    def terminate(self) -> None:
        """Public idempotent cleanup hook used during worker shutdown."""
        self._terminate()

    def _terminate(self) -> None:
        """Terminate the process group, then force-kill any survivors."""
        with self._terminate_lock:
            process = self.process
            if process is None or self._termination_complete:
                return
            self._termination_complete = True

            tracked: list[psutil.Process] = []
            try:
                parent = psutil.Process(process.pid)
                tracked = parent.children(recursive=True) + [parent]
            except psutil.NoSuchProcess:
                parent = None

            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                if parent is not None:
                    try:
                        parent.terminate()
                    except psutil.Error:
                        pass

            if tracked:
                _, alive = psutil.wait_procs(
                    tracked,
                    timeout=self.terminate_grace_seconds,
                )
                for survivor in alive:
                    try:
                        survivor.kill()
                    except psutil.Error:
                        pass
                if alive:
                    psutil.wait_procs(alive, timeout=self.terminate_grace_seconds)

            try:
                process.wait(timeout=self.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
                process.wait(timeout=self.terminate_grace_seconds)

    @property
    def pid(self) -> Optional[int]:
        return self.process.pid if self.process else None


class WorktreeUnsupportedError(RuntimeError):
    """A ``git worktree`` operation could not be completed for this repo.

    Raised for any failure of ``git worktree add`` (missing git, repo not
    initialized, worktree feature disabled by config, path collision that
    survives a cleanup attempt, ...). Callers are expected to catch this and
    fall back to running directly in the shared repo root.
    """


class WorktreeManager:
    """Create and tear down one isolated ``git worktree`` per agent run.

    Reconciliation strategy (CTV2-105): each run gets its own branch
    (``ct-run/<run_id>``) created from the run's base ref. A worktree is a
    separate checkout with its own index and HEAD, so concurrent runs never
    contend on the main repo's ``.git/index.lock``, and a run's commits never
    move another run's (or the primary checkout's) HEAD out from under it.
    Because a worktree shares the parent repo's object database, any commit
    made on ``ct-run/<run_id>`` is immediately reachable from ``repo_root``
    (``git log --all``, ``git show <sha>``, ...) even after the worktree
    directory itself is removed -- no merge is required for the commit to be
    durable. Folding that branch into the task's target branch (merge,
    fast-forward, or cherry-pick) is left to a later, explicit reconciliation
    step: doing it here would mean force-moving a ref that may be checked
    out in the primary worktree, which is exactly the unsafe concurrent
    mutation this feature removes.
    """

    def __init__(self, repo_root: str, *, worktree_root: Optional[str] = None) -> None:
        self.repo_root = repo_root
        self.worktree_root = worktree_root or self._default_worktree_root(repo_root)

    @staticmethod
    def _default_worktree_root(repo_root: str) -> str:
        # Kept outside repo_root's own working tree on purpose: a nested
        # checkout under the repo would show up as an untracked directory in
        # the primary worktree's `git status`, tripping the dirty-repo
        # warning/checks for unrelated reasons.
        digest = hashlib.sha1(os.path.abspath(repo_root).encode("utf-8")).hexdigest()[:16]
        return os.path.join(tempfile.gettempdir(), "control-tower-worktrees", digest)

    def branch_name(self, run_id: str) -> str:
        return f"ct-run/{run_id}"

    def worktree_path(self, run_id: str) -> str:
        return os.path.join(self.worktree_root, run_id)

    def create(self, run_id: str, base_ref: str) -> str:
        """Create (or recreate) the worktree for ``run_id`` at ``base_ref``.

        Raises :class:`WorktreeUnsupportedError` on any failure; the caller
        decides whether/how to fall back.
        """
        path = self.worktree_path(run_id)
        branch = self.branch_name(run_id)

        # A redelivered message after a hard crash can leave a stale entry
        # behind; clear it before trying again rather than failing forever.
        self._force_remove(path)
        self._delete_branch_if_exists(branch)

        try:
            os.makedirs(self.worktree_root, exist_ok=True)
        except OSError as exc:
            raise WorktreeUnsupportedError(
                f"Could not create worktree root {self.worktree_root}: {exc}"
            ) from exc

        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, path, base_ref],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            self._force_remove(path)
            self._delete_branch_if_exists(branch)
            raise WorktreeUnsupportedError(
                (result.stderr or result.stdout or "git worktree add failed").strip()
            )
        return path

    def remove(self, run_id_or_path: str) -> None:
        """Remove a worktree, tolerating a path that is already gone.

        Never raises: cleanup runs from ``finally`` blocks on every exit
        path (success, failure, timeout, cancellation, crash) and must not
        itself become a source of lost/hanging runs.
        """
        path = (
            run_id_or_path
            if os.path.isabs(run_id_or_path)
            else self.worktree_path(run_id_or_path)
        )
        self._force_remove(path)

    def _force_remove(self, path: str) -> None:
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", path],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("git worktree remove failed for %s", path, exc_info=True)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

    def _delete_branch_if_exists(self, branch: str) -> None:
        try:
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def supported(self) -> bool:
        """Best-effort capability probe used for diagnostics/logging only."""
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
