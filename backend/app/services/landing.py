"""Land a reviewed result onto the project's integration branch (CTV2-238).

"done" must mean the code is actually on main. After a pass verdict the
system — never the coordinator LLM — merges the executor's head commit into
whatever branch ``repo_root`` has checked out, records the merge commit as
``landed_ref``, and deletes the now-merged ``ct-run/*`` branches.

Pure git subprocess work: no LLM call, no tokens. A merge that cannot be
done safely (conflict, dirty tree, detached HEAD) is reported as a failure
so the caller can escalate to a human instead of silently claiming success.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 60


@dataclass
class LandingResult:
    ok: bool
    landed_ref: str | None = None
    skipped_reason: str | None = None
    error: str | None = None

    @property
    def skipped(self) -> bool:
        return self.ok and self.landed_ref is None


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def head_of(result_ref: str | None) -> str | None:
    """Extract the head commit from a ``<base>..<head>`` result_ref."""
    if not result_ref or ".." not in result_ref:
        return None
    head = result_ref.rsplit("..", 1)[-1].strip()
    return head or None


def land_result(repo_root: str, head: str, message: str) -> LandingResult:
    """Merge ``head`` into the branch checked out at ``repo_root``.

    Returns:
    - ok + landed_ref: merged (or already an ancestor — idempotent retry).
    - ok + skipped_reason: landing does not apply here (not a git repo /
      unparseable head). The caller may complete the task without landing —
      this keeps non-git test fixtures and legacy imports working.
    - not ok + error: landing applies but FAILED (conflict, dirty tree,
      detached HEAD, ...). The caller must NOT mark the task done.
    """
    probe = _git(repo_root, "rev-parse", "--git-dir")
    if probe.returncode != 0:
        return LandingResult(ok=True, skipped_reason=f"{repo_root} is not a git repository")

    exists = _git(repo_root, "cat-file", "-e", f"{head}^{{commit}}")
    if exists.returncode != 0:
        return LandingResult(ok=False, error=f"head commit {head!r} does not exist in {repo_root}")

    branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch.returncode != 0 or branch.stdout.strip() == "HEAD":
        return LandingResult(
            ok=False,
            error="repo is on a detached HEAD; check out the integration branch first",
        )

    # Idempotent: an earlier attempt (or a human) may have merged already.
    ancestor = _git(repo_root, "merge-base", "--is-ancestor", head, "HEAD")
    if ancestor.returncode == 0:
        current = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
        _cleanup_merged_run_branches(repo_root)
        return LandingResult(ok=True, landed_ref=current)

    # Tracked modifications block a merge; untracked files are fine unless
    # the merge itself collides with them (git aborts on its own then).
    dirty = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    if dirty.stdout.strip():
        return LandingResult(
            ok=False,
            error=(
                "repo working tree has uncommitted tracked changes; "
                "commit or stash them before landing"
            ),
        )

    merge = _git(repo_root, "merge", "--no-ff", head, "-m", message)
    if merge.returncode != 0:
        _git(repo_root, "merge", "--abort")
        detail = (merge.stderr.strip() or merge.stdout.strip())[:500]
        return LandingResult(ok=False, error=f"merge failed: {detail}")

    landed = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
    _cleanup_merged_run_branches(repo_root)
    return LandingResult(ok=True, landed_ref=landed)


def _cleanup_merged_run_branches(repo_root: str) -> None:
    """Delete ct-run/* branches whose tip is already reachable from HEAD."""
    try:
        refs = _git(
            repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads/ct-run"
        )
        for name in refs.stdout.split():
            merged = _git(repo_root, "merge-base", "--is-ancestor", name, "HEAD")
            if merged.returncode == 0:
                _git(repo_root, "branch", "-D", name)
        _git(repo_root, "worktree", "prune")
    except Exception:  # cleanup must never break a successful landing
        logger.exception("ct-run branch cleanup failed in %s", repo_root)
