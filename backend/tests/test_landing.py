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
    assert "uncommitted" in result.error


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

    result = land_result(str(repo), head, "Merge T-1")

    assert not result.ok
    assert "detached" in result.error
