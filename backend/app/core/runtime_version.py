"""Detect when the running backend no longer matches the repository HEAD or DB schema.

The monitor deliberately does one thing: remember HEAD when the MCP process
boots, then compare that SHA with HEAD on demand, and compare DB alembic revision
with script directory head revisions. It never reloads code or
restarts a process; deciding when it is safe to restart belongs to the human
operator because workers may have live CLI runs.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alembic.script import ScriptDirectory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
    alembic_dir: Path | None = None

    @classmethod
    def capture(
        cls,
        repo_root: Path = REPOSITORY_ROOT,
        alembic_dir: Path | None = None,
    ) -> RuntimeVersionMonitor:
        root = Path(repo_root).resolve()
        boot_commit = _git(root, "rev-parse", "HEAD")
        if boot_commit:
            logger.info("Backend boot commit: %s", boot_commit)
        else:
            logger.warning("Could not record backend boot commit from %s", root)
        return cls(repo_root=root, boot_commit=boot_commit, alembic_dir=alembic_dir)

    def stale_warning(self, db: Session | None = None) -> dict[str, Any] | None:
        """Return a warning when current HEAD contains code absent at boot, or DB schema is behind script directory heads."""

        # 1. Check Git SHA
        current_commit = _git(self.repo_root, "rev-parse", "HEAD") if self.boot_commit else None
        code_stale = bool(self.boot_commit and current_commit and current_commit != self.boot_commit)
        pending_commit_count: int | None = None
        if code_stale:
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

        # 2. Check Alembic Revisions
        schema_stale = False
        missing_revisions: list[str] = []
        db_revisions: list[str] = []
        script_heads: list[str] = []

        alembic_path = self.alembic_dir or (self.repo_root / "backend" / "alembic")
        if alembic_path.exists():
            try:
                script_dir = ScriptDirectory(str(alembic_path))
                script_heads = script_dir.get_heads()
            except Exception as exc:
                logger.warning("Could not read alembic script directory at %s: %s", alembic_path, exc)
                script_heads = []

        if script_heads:
            db_session = db
            close_session = False
            if db_session is None:
                try:
                    from app.db.base import SessionLocal

                    db_session = SessionLocal()
                    close_session = True
                except Exception:
                    db_session = None

            if db_session is not None:
                try:
                    db_revisions = [
                        str(r)
                        for r in db_session.execute(
                            text("SELECT version_num FROM alembic_version")
                        )
                        .scalars()
                        .all()
                    ]
                except Exception as exc:
                    logger.warning("Could not query alembic_version table: %s", exc)
                    db_revisions = []
                finally:
                    if close_session:
                        db_session.close()

            db_rev_set = set(db_revisions)
            head_set = set(script_heads)
            if not head_set.issubset(db_rev_set):
                schema_stale = True
                missing = []
                try:
                    for rev in script_dir.walk_revisions():
                        if rev.revision in db_rev_set:
                            break
                        missing.append(rev.revision)
                except Exception:
                    missing = [h for h in script_heads if h not in db_rev_set]
                missing_revisions = missing

        if not code_stale and not schema_stale:
            return None

        warning: dict[str, Any] = {
            "code": "runtime_restart_required",
            "code_stale": code_stale,
            "schema_stale": schema_stale,
        }

        if self.boot_commit:
            warning["boot_commit"] = self.boot_commit
        if current_commit:
            warning["current_commit"] = current_commit
        if pending_commit_count is not None:
            warning["pending_commit_count"] = pending_commit_count

        if schema_stale:
            warning["missing_revisions"] = missing_revisions
            warning["db_revisions"] = db_revisions
            warning["script_heads"] = script_heads

        messages = []
        recommendations = []

        if code_stale:
            count_label = (
                f"{pending_commit_count} commit"
                if pending_commit_count is not None
                else "một số commit"
            )
            messages.append(
                f"Backend/worker đang chạy code cũ: {count_label} trên HEAD "
                "chưa có hiệu lực. Code đã vào main nhưng CHƯA có hiệu lực "
                "cho tới khi human restart backend/worker vào thời điểm an toàn."
            )
            recommendations.append(
                "Kiểm tra các AgentRun đang chạy rồi chọn thời điểm restart;"
            )

        if schema_stale:
            missing_str = (
                ", ".join(missing_revisions)
                if missing_revisions
                else "không xác định"
            )
            messages.append(
                f"Database schema đang thiếu alembic revision: {missing_str} "
                f"(head: {', '.join(script_heads)}, DB: {', '.join(db_revisions) or 'none'}). "
                "Migration chưa được áp dụng."
            )
            recommendations.append("Chạy alembic upgrade khi an toàn;")

        recommendations.append("hệ thống sẽ không tự động restart.")

        warning["message"] = " ".join(messages)
        warning["recommendation"] = " ".join(recommendations)

        return warning

