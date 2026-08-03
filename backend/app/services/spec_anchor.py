"""Symbol anchoring and commit-triggered staleness for the living spec (CTV2-1342).

Design: ``docs/spec/08-living-spec.md`` section "Cơ chế mất hiệu lực". The
whole module is deterministic subprocess/string work -- no LLM call anywhere.
An LLM is never asked "is this still true"; that question is answered here,
in code, from the git history.

``anchor_sha`` is a hash of one symbol's source block, computed the same way
at anchor time (``compute_anchor_sha``) and at invalidation time
(``apply_commit_staleness``) so the two are directly comparable. Symbol
extraction is a best-effort, language-agnostic heuristic (locate the
definition line, then capture its body by indentation or brace depth) -- it
does not require a language-specific parser.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess

from sqlalchemy.orm import Session

from app.db.models import SpecAnchor, SpecItem

_GIT_TIMEOUT_SECONDS = 30

_DEF_LINE_RE = (
    r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:def|class|function|interface|type|const|let|var)\s+{name}\b'
)


def _symbol_name(symbol: str) -> str:
    """The leaf name of a dotted/namespaced symbol, e.g. ``Foo.bar`` -> ``bar``."""
    return symbol.rsplit(".", 1)[-1].rsplit("::", 1)[-1].strip()


def extract_symbol_source(text: str, symbol: str) -> str | None:
    """Best-effort extraction of one symbol's source block from file `text`.

    Returns ``None`` when no definition line matches -- callers treat that as
    "symbol vanished" (also a form of staleness).
    """
    name = _symbol_name(symbol)
    if not name:
        return None
    pattern = re.compile(_DEF_LINE_RE.format(name=re.escape(name)))
    lines = text.splitlines()
    start = next((idx for idx, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        return None

    start_line = lines[start]
    indent = len(start_line) - len(start_line.lstrip(" \t"))

    if "{" in start_line:
        depth = 0
        end = start
        opened = False
        for idx in range(start, len(lines)):
            depth += lines[idx].count("{") - lines[idx].count("}")
            opened = opened or "{" in lines[idx]
            end = idx
            if opened and depth <= 0:
                break
        return "\n".join(lines[start : end + 1])

    end = start
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        if not line.strip():
            end = idx
            continue
        line_indent = len(line) - len(line.lstrip(" \t"))
        if line_indent <= indent:
            break
        end = idx
    return "\n".join(lines[start : end + 1])


def hash_symbol_source(source: str) -> str:
    normalized = "\n".join(line.rstrip() for line in source.strip("\n").splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _read_file_at(repo_root: str, path: str, commit_sha: str | None) -> str | None:
    """Read `path` at `commit_sha` (git object), or the working tree if None."""
    if commit_sha:
        try:
            proc = subprocess.run(
                ["git", "-C", repo_root, "show", f"{commit_sha}:{path}"],
                capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout
    try:
        with open(os.path.join(repo_root, path), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def compute_anchor_sha(
    repo_root: str, path: str, symbol: str, commit_sha: str | None = None
) -> str | None:
    """Hash of `symbol`'s current source block in `path`, or None if unresolvable."""
    text = _read_file_at(repo_root, path, commit_sha)
    if text is None:
        return None
    source = extract_symbol_source(text, symbol)
    if source is None:
        return None
    return hash_symbol_source(source)


def _diff_range(commit_sha: str) -> tuple[str, str]:
    """The (before, after) revisions to diff for one commit event.

    A ``<base>..<head>`` result_ref (worktree branch, pre-landing) is used
    verbatim as a range. A single sha (e.g. `land_task`'s --no-ff merge
    commit) is diffed against its first parent, which is exactly the set of
    changes the merge introduced.
    """
    if ".." in commit_sha:
        base, head = commit_sha.split("..", 1)
        return base.strip(), head.strip()
    return f"{commit_sha}^1", commit_sha


def _changed_paths(repo_root: str, before: str, after: str) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "diff", "--name-only", before, after],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode != 0:
        return set()
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def apply_commit_staleness(
    db: Session, project_id: str, repo_root: str, commit_sha: str
) -> dict:
    """Mark spec_item stale when `commit_sha` touches one of its anchored symbols.

    Pure code, no LLM. Called from the same `graph_rebuild_requested` outbox
    event CTV2-1339 uses to trigger incremental graph rebuild (see
    `app.services.outbox`) -- one event source, two side effects.

    Idempotent: re-running with the same (repo_root, commit_sha) recomputes
    the same diff and the same per-anchor hash comparison, so a replayed
    outbox message produces the same status/reason rather than a new effect.
    """
    result = {"checked": 0, "staled": []}
    if not repo_root or not commit_sha:
        return result

    before, after = _diff_range(commit_sha)
    changed_paths = _changed_paths(repo_root, before, after)
    if not changed_paths:
        return result

    anchors = (
        db.query(SpecAnchor)
        .join(SpecItem, SpecAnchor.spec_item_id == SpecItem.id)
        .filter(
            SpecAnchor.repo == repo_root,
            SpecAnchor.path.in_(changed_paths),
            SpecItem.project_id == project_id,
            SpecItem.archived_at.is_(None),
        )
        .all()
    )
    result["checked"] = len(anchors)
    for anchor in anchors:
        current_sha = compute_anchor_sha(repo_root, anchor.path, anchor.symbol, commit_sha=after)
        if current_sha == anchor.anchor_sha:
            continue
        item = db.get(SpecItem, anchor.spec_item_id)
        if item is None or item.archived_at is not None:
            continue
        reason = (
            f"symbol '{anchor.symbol}' in {anchor.path} changed at commit {after} "
            f"(anchor_sha {anchor.anchor_sha[:12]} -> "
            f"{current_sha[:12] if current_sha else 'missing'})"
        )
        item.status = "stale"
        item.stale_reason = reason
        result["staled"].append(
            {"spec_item_id": item.id, "symbol": anchor.symbol, "path": anchor.path, "reason": reason}
        )
    db.commit()
    return result
