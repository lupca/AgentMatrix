"""Land a reviewed result onto the project's integration branch (CTV2-238).

"done" must mean the code is actually on main. After a pass verdict the
system — never the coordinator LLM — merges the executor's head commit into
whatever branch ``repo_root`` has checked out, records the merge commit as
``landed_ref``, and deletes the now-merged ``ct-run/*`` branches.

Pure git subprocess work: no LLM call, no tokens. A merge that cannot be
done safely (conflict, dirty tree, detached HEAD, multiple alembic heads)
is reported as a failure so the caller can escalate to a human instead of
silently claiming success.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 60


def _check_alembic_heads(repo_root: str) -> tuple[bool, list[str], str | None]:
    """Check if alembic has exactly one head revision.

    Returns:
        (ok, heads, error): ok=True if single head, heads is the list of head
        revision IDs, error is a descriptive message if >1 head.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        return (True, [], None)

    alembic_ini = os.path.join(repo_root, "backend", "alembic.ini")
    alembic_dir = os.path.join(repo_root, "backend", "alembic")

    if not os.path.isfile(alembic_ini) or not os.path.isdir(alembic_dir):
        return (True, [], None)

    try:
        cfg = Config(alembic_ini)
        cfg.set_main_option("script_location", alembic_dir)
        script = ScriptDirectory.from_config(cfg)
        heads = list(script.get_heads())

        if len(heads) <= 1:
            return (True, heads, None)

        rev_details = []
        for rev_id in heads:
            rev = script.get_revision(rev_id)
            filename = os.path.basename(rev.path) if rev and rev.path else rev_id
            rev_details.append(f"  - {filename} ({rev_id[:12]})")

        error = (
            f"Multiple alembic heads detected ({len(heads)}). "
            "This happens when parallel tasks add migrations with the same down_revision. "
            "Create a merge revision before landing:\n"
            f"  alembic merge heads -m 'merge ...'\n"
            f"Conflicting revisions:\n" + "\n".join(rev_details)
        )
        return (False, heads, error)
    except Exception as e:
        logger.warning("alembic head check failed: %s", e)
        return (True, [], None)


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
        head_sha = _git(repo_root, "rev-parse", "--short", "HEAD").stdout.strip()
        sha_info = f" (at {head_sha})" if head_sha else ""
        return LandingResult(
            ok=False,
            error=(
                f"repo is on a detached HEAD{sha_info}; "
                "check out the integration branch first using: git checkout main"
            ),
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
        dirty_lines = [line.strip() for line in dirty.stdout.strip().splitlines() if line.strip()]
        dirty_files = [line.split(maxsplit=1)[-1] for line in dirty_lines]
        files_str = ", ".join(dirty_files[:5])
        if len(dirty_files) > 5:
            files_str += f" (+{len(dirty_files) - 5} more)"
        return LandingResult(
            ok=False,
            error=(
                f"repo working tree has uncommitted tracked changes in: {files_str}; "
                "commit or stash them before landing using: git stash"
            ),
        )

    merge = _git(repo_root, "merge", "--no-ff", "--no-commit", head)
    if merge.returncode != 0:
        _git(repo_root, "merge", "--abort")
        detail = (merge.stderr.strip() or merge.stdout.strip())[:500]
        return LandingResult(ok=False, error=f"merge failed: {detail}")

    heads_ok, heads, heads_error = _check_alembic_heads(repo_root)
    if not heads_ok:
        _git(repo_root, "merge", "--abort")
        return LandingResult(ok=False, error=heads_error)

    commit = _git(repo_root, "commit", "-m", message)
    if commit.returncode != 0:
        _git(repo_root, "merge", "--abort")
        detail = (commit.stderr.strip() or commit.stdout.strip())[:500]
        return LandingResult(ok=False, error=f"commit failed: {detail}")

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
