"""Detect when the running backend no longer matches the repository HEAD.

The monitor deliberately does one thing: remember HEAD when the MCP process
boots, then compare that SHA with HEAD on demand.  It never reloads code or
restarts a process; deciding when it is safe to restart belongs to the human
operator because workers may have live CLI runs.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/app/core/runtime_version.py -> repository root
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


@dataclass(frozen=True)
class RuntimeVersionMonitor:
    """A process-local snapshot of the repository revision at boot."""

    repo_root: Path
    boot_commit: str | None

    @classmethod
    def capture(cls, repo_root: Path = REPOSITORY_ROOT) -> RuntimeVersionMonitor:
        root = Path(repo_root).resolve()
        boot_commit = _git(root, "rev-parse", "HEAD")
        if boot_commit:
            logger.info("Backend boot commit: %s", boot_commit)
        else:
            logger.warning("Could not record backend boot commit from %s", root)
        return cls(repo_root=root, boot_commit=boot_commit)

    def stale_warning(self) -> dict[str, object] | None:
        """Return a warning when current HEAD contains code absent at boot."""

        if not self.boot_commit:
            return None
        current_commit = _git(self.repo_root, "rev-parse", "HEAD")
        if not current_commit or current_commit == self.boot_commit:
            return None

        count_text = _git(
            self.repo_root,
            "rev-list",
            "--count",
            f"{self.boot_commit}..{current_commit}",
        )
        try:
            pending_commit_count = int(count_text) if count_text is not None else None
        except ValueError:
            pending_commit_count = None

        count_label = (
            f"{pending_commit_count} commit"
            if pending_commit_count is not None
            else "một số commit"
        )
        return {
            "code": "runtime_restart_required",
            "boot_commit": self.boot_commit,
            "current_commit": current_commit,
            "pending_commit_count": pending_commit_count,
            "message": (
                f"Backend/worker đang chạy code cũ: {count_label} trên HEAD "
                "chưa có hiệu lực. Code đã vào main nhưng CHƯA có hiệu lực "
                "cho tới khi human restart backend/worker vào thời điểm an toàn."
            ),
            "recommendation": (
                "Kiểm tra các AgentRun đang chạy rồi chọn thời điểm restart; "
                "hệ thống sẽ không tự động restart."
            ),
        }
