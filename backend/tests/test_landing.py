"""Tests for services.landing — landing a reviewed result onto main (CTV2-238)."""

from __future__ import annotations

import subprocess

import pytest

from app.services.landing import head_of, land_result


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@t"),
        ("config", "user.name", "t"),
    ):
        assert _git(root, *args).returncode == 0
    (root / "a.txt").write_text("base\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _run_branch_commit(repo, branch="ct-run/run-1", filename="feature.txt"):
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).write_text("feature\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")
    return base, head


def test_head_of_parses_result_ref():
    assert head_of("abc..def") == "def"
    assert head_of("abc123..def456..") is None or head_of("abc..def") == "def"
    assert head_of(None) is None
    assert head_of("legacy-migration") is None


def test_clean_merge_lands_and_deletes_run_branch(repo):
    _, head = _run_branch_commit(repo)

    result = land_result(str(repo), head, "Merge T-1: feature")

    assert result.ok and result.landed_ref
    assert (repo / "feature.txt").exists()
    # merge commit is on main and the ct-run branch is gone
    assert _git(repo, "merge-base", "--is-ancestor", head, "HEAD").returncode == 0
    branches = _git(repo, "branch", "--list", "ct-run/*").stdout.strip()
    assert branches == ""


def test_landing_is_idempotent(repo):
    _, head = _run_branch_commit(repo)
    first = land_result(str(repo), head, "Merge T-1")
    second = land_result(str(repo), head, "Merge T-1 again")

    assert first.ok and second.ok
    assert second.landed_ref  # already an ancestor -> reports current HEAD


def test_conflict_fails_and_leaves_repo_clean(repo):
    _, head = _run_branch_commit(repo, filename="a.txt")  # branch rewrites a.txt
    (repo / "a.txt").write_text("conflicting main change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "conflicting")

    result = land_result(str(repo), head, "Merge T-1")

    assert not result.ok
    assert "merge failed" in result.error
    # merge was aborted: no MERGE_HEAD, tree clean
    assert _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_dirty_tracked_tree_refuses_to_land(repo):
    _, head = _run_branch_commit(repo)
    (repo / "a.txt").write_text("uncommitted edit\n")

    result = land_result(str(repo), head, "Merge T-1")

    assert not result.ok
    assert "uncommitted tracked changes in: a.txt" in result.error
    assert "git stash" in result.error


def test_untracked_files_do_not_block_landing(repo):
    _, head = _run_branch_commit(repo)
    (repo / "scratch.log").write_text("untracked\n")

    result = land_result(str(repo), head, "Merge T-1")

    assert result.ok and result.landed_ref


def test_non_git_repo_root_is_a_skip_not_a_failure(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    result = land_result(str(plain), "deadbeef", "Merge T-1")

    assert result.ok and result.skipped
    assert "not a git repository" in result.skipped_reason


def test_missing_head_commit_fails(repo):
    result = land_result(str(repo), "deadbeef", "Merge T-1")

    assert not result.ok
    assert "does not exist" in result.error


def test_detached_head_fails(repo):
    _, head = _run_branch_commit(repo)
    _git(repo, "checkout", "-q", "--detach")
    detached_sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    result = land_result(str(repo), head, "Merge T-1")

    assert not result.ok
    assert f"detached HEAD (at {detached_sha})" in result.error
    assert "git checkout main" in result.error


@pytest.fixture
def alembic_repo(tmp_path):
    """A repo with backend/alembic structure and a base migration."""
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@t"),
        ("config", "user.name", "t"),
    ):
        assert _git(root, *args).returncode == 0

    backend = root / "backend"
    backend.mkdir()
    alembic_dir = backend / "alembic"
    alembic_dir.mkdir()
    versions_dir = alembic_dir / "versions"
    versions_dir.mkdir()

    (backend / "alembic.ini").write_text(
        "[alembic]\nscript_location = backend/alembic\n"
    )
    (alembic_dir / "script.py.mako").write_text("")
    (alembic_dir / "env.py").write_text(
        "from alembic import context\n"
        "def run_migrations_online(): pass\n"
        "if not context.is_offline_mode(): run_migrations_online()\n"
    )

    (versions_dir / "001_base.py").write_text(
        'revision = "001"\ndown_revision = None\ndef upgrade(): pass\ndef downgrade(): pass\n'
    )

    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base with alembic")
    return root


def test_multiple_alembic_heads_blocks_landing(alembic_repo):
    """Landing is blocked when merge would create multiple alembic heads (CTV2-1347)."""
    root = alembic_repo
    versions_dir = root / "backend" / "alembic" / "versions"

    _git(root, "checkout", "-q", "-b", "ct-run/task-a")
    (versions_dir / "002_feature_a.py").write_text(
        'revision = "002a"\ndown_revision = "001"\ndef upgrade(): pass\ndef downgrade(): pass\n'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add migration 002a")
    head_a = _git(root, "rev-parse", "HEAD").stdout.strip()
    _git(root, "checkout", "-q", "main")

    _git(root, "checkout", "-q", "-b", "ct-run/task-b")
    (versions_dir / "002_feature_b.py").write_text(
        'revision = "002b"\ndown_revision = "001"\ndef upgrade(): pass\ndef downgrade(): pass\n'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add migration 002b")
    _git(root, "checkout", "-q", "main")

    first = land_result(str(root), head_a, "Merge task-a")
    assert first.ok and first.landed_ref, f"first merge should succeed: {first}"

    head_b = _git(root, "rev-parse", "ct-run/task-b").stdout.strip()
    second = land_result(str(root), head_b, "Merge task-b")

    assert not second.ok
    assert "Multiple alembic heads" in second.error
    assert "002a" in second.error or "002_feature_a" in second.error
    assert "002b" in second.error or "002_feature_b" in second.error

    assert _git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0
    status = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    assert status == "", f"repo has uncommitted tracked changes: {status}"
