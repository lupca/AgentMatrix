"""Symbol anchoring and commit-triggered staleness for the living spec (CTV2-1342).

Design: ``docs/spec/08-living-spec.md`` section "Cơ chế mất hiệu lực". The
whole module is deterministic subprocess/string work -- no LLM call anywhere.
An LLM is never asked "is this still true"; that question is answered here,
in code, from the git history.

``anchor_sha`` is a hash of the anchored content, computed the same way at
anchor time (``compute_anchor_sha``) and at invalidation time
(``apply_commit_staleness``) so the two are directly comparable. Python files
use AST declarations. Configuration and other non-Python files use a
whole-file hash because the existing symbol extractor cannot define a stable
source block for those formats. This distinction is derived from the path, so
it does not require a schema migration.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import subprocess
import uuid
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.db.models import Project, SpecAnchor, SpecItem, SpecTaskLink, Task


def resolve_repo_root(db: Session | None, repo: str) -> str:
    """Turn an anchor's ``repo`` value into a directory that can be read.

    ``SpecAnchor.repo`` holds a *project id* ("agenticmatix"), but every reader
    passed it straight to ``open()`` as if it were a path. Nothing ever resolved
    it, so the whole anchoring mechanism was inert in both directions:

      * writing  — ``compute_anchor_sha`` always returned None, so ``spec_write``
        fell through to the agent-supplied ``anchor_sha``. That fallback is how
        283 anchors ended up holding 40-char commit SHAs: agents had no other
        option, whatever the tool description and the prompt claimed.
      * reading  — ``apply_commit_staleness`` filters ``SpecAnchor.repo ==
        repo_root``, comparing a project id against an absolute path. It matched
        nothing, every time. Across 862 anchors and hundreds of commits, not one
        spec_item was ever marked stale.

    Accepts either form: an existing directory is returned unchanged, so callers
    that already hold a real ``repo_root`` (``apply_commit_staleness``) keep
    working, and so do tests that pass a tmp_path.
    """
    if not repo:
        return repo
    if os.path.isdir(repo):
        return repo
    if db is None:
        return repo
    project = db.get(Project, repo)
    root = (getattr(project, "repo_root", None) or "").strip() if project else ""
    return root or repo

_GIT_TIMEOUT_SECONDS = 30
_PYTHON_SUFFIX = ".py"

_DEF_LINE_RE = (
    r'^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?'
    r'(?:def|class|function|interface|type|const|let|var)\s+{name}\b'
)


def _symbol_name(symbol: str) -> str:
    """The leaf name of a dotted/namespaced symbol, e.g. ``Foo.bar`` -> ``bar``."""
    return symbol.rsplit(".", 1)[-1].rsplit("::", 1)[-1].strip()


def is_python_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() == _PYTHON_SUFFIX


def anchor_mode(path: str) -> str:
    """Return the content mode used for both writing and invalidation."""
    return "python-symbol" if is_python_path(path) else "whole-file"


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


def _python_node_source(text: str, node: ast.AST) -> str | None:
    source = ast.get_source_segment(text, node)
    if source is not None:
        return source
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None or end is None:
        return None
    return "\n".join(text.splitlines()[start - 1 : end])


def _assignment_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return set()

    names: set[str] = set()

    def visit(target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                visit(element)

    for target in targets:
        visit(target)
    return names


def _python_declaration(node: ast.AST, name: str) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    return name in _assignment_names(node)


def extract_python_symbol_source(text: str, symbol: str) -> str | None:
    """Extract a local Python declaration, including assignments and class attrs.

    Imported names, call-site references, constraint names, and comma-separated
    pseudo-symbols are intentionally not accepted: they do not identify source
    owned by this file and would make staleness silently meaningless.
    """
    if not symbol or "," in symbol:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    if "." in symbol:
        owner, member = symbol.rsplit(".", 1)
        owner = _symbol_name(owner)
        member = member.strip()
        if not owner or not member:
            return None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == owner:
                for child in node.body:
                    if _python_declaration(child, member):
                        return _python_node_source(text, child)
        return None

    name = symbol.strip()
    if not name:
        return None
    for node in ast.walk(tree):
        if _python_declaration(node, name):
            return _python_node_source(text, node)
    return None


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


def source_available(repo_root: str, path: str) -> bool:
    """Whether the current working tree contains the requested source file."""
    return _read_file_at(repo_root, path, None) is not None


def compute_anchor_sha(
    repo_root: str, path: str, symbol: str, commit_sha: str | None = None
) -> str | None:
    """Hash the canonical content selected by :func:`anchor_mode`."""
    text = _read_file_at(repo_root, path, commit_sha)
    if text is None:
        return None
    if anchor_mode(path) == "whole-file":
        return hash_symbol_source(text)
    source = extract_python_symbol_source(text, symbol)
    return hash_symbol_source(source) if source is not None else None


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


def link_task_to_changed_specs(
    db: Session,
    task: Task,
    repo_root: str,
    commit_ref: str,
) -> list[SpecTaskLink]:
    """Derive idempotent task→spec links from landed changed paths.

    A landing already has an immutable reviewed ``base..head`` range. Matching
    that range's changed files against project-local anchors is deterministic,
    requires no LLM, and gives future planners the missing delivery-history
    edge. Multiple anchors for one spec still produce one ``modifies`` link.

    The caller owns the transaction. This function explicitly flushes because
    the application session uses ``autoflush=False``.
    """
    if not task.project or not repo_root or not commit_ref:
        return []

    before, after = _diff_range(commit_ref)
    changed_paths = _changed_paths(repo_root, before, after)
    if not changed_paths:
        return []

    normalized_repo = os.path.abspath(repo_root)
    repo_candidates = {repo_root, normalized_repo}
    anchored_item_ids = {
        item_id
        for (item_id,) in (
            db.query(SpecAnchor.spec_item_id)
            .join(SpecItem, SpecAnchor.spec_item_id == SpecItem.id)
            .filter(
                SpecAnchor.repo.in_(repo_candidates),
                SpecAnchor.path.in_(changed_paths),
                SpecItem.project_id == task.project,
                SpecItem.archived_at.is_(None),
            )
            .all()
        )
    }
    if not anchored_item_ids:
        return []

    existing_item_ids = {
        item_id
        for (item_id,) in (
            db.query(SpecTaskLink.spec_item_id)
            .filter(
                SpecTaskLink.task_id == task.id,
                SpecTaskLink.relation == "modifies",
                SpecTaskLink.spec_item_id.in_(anchored_item_ids),
            )
            .all()
        )
    }
    links = [
        SpecTaskLink(
            id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"landed-spec-link:{task.id}:{item_id}:modifies",
                )
            ),
            spec_item_id=item_id,
            task_id=task.id,
            relation="modifies",
            confidence="derived",
            created_by="system:landing",
        )
        for item_id in sorted(anchored_item_ids - existing_item_ids)
    ]
    if links:
        db.add_all(links)
        db.flush()
    return links


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
            # Match on how `repo` is actually stored — a project id — while
            # still accepting a raw path, since callers and tests use both.
            # Comparing only against `repo_root` matched nothing: every anchor
            # holds "agenticmatix", never "/home/.../agenticmatix". That single
            # mismatch is why no spec_item had ever been marked stale.
            SpecAnchor.repo.in_({repo_root, project_id}),
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
        target_label = "file" if anchor_mode(anchor.path) == "whole-file" else "symbol"
        reason = (
            f"{target_label} '{anchor.symbol}' in {anchor.path} changed at commit {after} "
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
