"""Subprocess lifecycle management for background agent executions."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Generator, Optional

import psutil


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
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self.timeout = timeout_seconds
        self.terminate_grace_seconds = terminate_grace_seconds
        self.poll_interval = poll_interval
        self.cancel_check = cancel_check
        self.on_start = on_start
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

                elapsed = time.monotonic() - started_at
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
