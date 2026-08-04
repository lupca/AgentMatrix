"""Regression tests for detecting code landed after the backend booted."""

from __future__ import annotations

import subprocess

from app.core.runtime_version import RuntimeVersionMonitor


def _git(repo, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit(repo, filename: str, content: str) -> str:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", f"update {filename}")
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path):
    repo = tmp_path / "agenticmatix"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "base.txt", "base\n")
    return repo


def test_matching_head_has_no_runtime_warning(tmp_path):
    repo = _repo(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo)

    assert monitor.boot_commit == _git(repo, "rev-parse", "HEAD")
    assert monitor.stale_warning() is None


def test_changed_head_reports_commits_not_effective_until_restart(tmp_path):
    repo = _repo(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo)
    _commit(repo, "one.txt", "one\n")
    current_commit = _commit(repo, "two.txt", "two\n")

    warning = monitor.stale_warning()

    assert warning is not None
    assert warning["boot_commit"] == monitor.boot_commit
    assert warning["current_commit"] == current_commit
    assert warning["pending_commit_count"] == 2
    assert "2 commit" in warning["message"]
    assert "CHƯA có hiệu lực" in warning["message"]
    assert "không tự động restart" in warning["recommendation"]
