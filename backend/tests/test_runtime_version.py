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


from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def _setup_repo_with_migrations(tmp_path):
    repo = _repo(tmp_path)
    alembic_dir = repo / "backend" / "alembic"
    versions_dir = alembic_dir / "versions"
    versions_dir.mkdir(parents=True)

    (versions_dir / "001_base.py").write_text(
        'revision = "001_base"\ndown_revision = None\n'
    )
    (versions_dir / "002_next.py").write_text(
        'revision = "002_next"\ndown_revision = "001_base"\n'
    )
    return repo, alembic_dir


def _setup_db_session(version: str | None):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        if version:
            conn.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": version}
            )
        conn.commit()
    return sessionmaker(bind=engine)()


def test_four_combinations_code_matching_schema_matching(tmp_path):
    repo, alembic_dir = _setup_repo_with_migrations(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo, alembic_dir=alembic_dir)
    db = _setup_db_session("002_next")

    warning = monitor.stale_warning(db=db)
    assert warning is None


def test_four_combinations_code_matching_schema_stale(tmp_path):
    repo, alembic_dir = _setup_repo_with_migrations(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo, alembic_dir=alembic_dir)
    db = _setup_db_session("001_base")

    warning = monitor.stale_warning(db=db)
    assert warning is not None
    assert warning["code_stale"] is False
    assert warning["schema_stale"] is True
    assert warning["missing_revisions"] == ["002_next"]
    assert warning["db_revisions"] == ["001_base"]
    assert warning["script_heads"] == ["002_next"]
    assert (
        "Database schema đang thiếu alembic revision: 002_next"
        in warning["message"]
    )


def test_four_combinations_code_stale_schema_matching(tmp_path):
    repo, alembic_dir = _setup_repo_with_migrations(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo, alembic_dir=alembic_dir)
    _commit(repo, "new_code.txt", "new\n")
    db = _setup_db_session("002_next")

    warning = monitor.stale_warning(db=db)
    assert warning is not None
    assert warning["code_stale"] is True
    assert warning["schema_stale"] is False
    assert warning["pending_commit_count"] == 1
    assert "Backend/worker đang chạy code cũ: 1 commit" in warning["message"]


def test_four_combinations_code_stale_schema_stale(tmp_path):
    repo, alembic_dir = _setup_repo_with_migrations(tmp_path)
    monitor = RuntimeVersionMonitor.capture(repo, alembic_dir=alembic_dir)
    _commit(repo, "new_code.txt", "new\n")
    db = _setup_db_session("001_base")

    warning = monitor.stale_warning(db=db)
    assert warning is not None
    assert warning["code_stale"] is True
    assert warning["schema_stale"] is True
    assert warning["pending_commit_count"] == 1
    assert warning["missing_revisions"] == ["002_next"]
    assert "Backend/worker đang chạy code cũ" in warning["message"]
    assert (
        "Database schema đang thiếu alembic revision: 002_next"
        in warning["message"]
    )

