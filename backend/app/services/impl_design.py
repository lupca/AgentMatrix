"""Implementation-design persistence and deterministic completeness checks.

This module deliberately contains no model or provider calls.  It answers only
mechanical questions about a design artifact: paths, graph symbols, required
fields, change-entry shape, and the git base it was authored from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.db.models import ImplDesign, Project, Task
from app.services.graph_client import symbol_exists


CHECK_NAMES = (
    "file_paths",
    "symbols",
    "test_plan",
    "non_goals",
    "change_entries",
    "derived_from_sha",
)


class ImplDesignError(ValueError):
    """A client-visible implementation-design validation error."""


def _nonempty(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _git_head(repo_root: str | None) -> str | None:
    if not repo_root:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _repo_path(repo_root: str | None, raw_path: Any) -> tuple[Path | None, str | None]:
    path = str(raw_path or "").strip()
    if not path:
        return None, "path is missing"
    if not repo_root:
        return None, f"file '{path}' cannot be checked because project.repo_root is not configured"
    root = Path(repo_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except ValueError:
        return None, f"file '{path}' is outside the project repository"
    return resolved, None


def _is_new_symbol(change: Mapping[str, Any]) -> bool:
    return bool(
        change.get("new") is True
        or change.get("is_new") is True
        or change.get("created") is True
        or str(change.get("action") or "").strip().lower() == "create"
    )


async def _symbol_exists(repo_root: str | None, symbol: str) -> bool:
    if not repo_root:
        return False
    return await symbol_exists(repo_root, symbol)


def _check(name: str, passed: bool, reason: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "reason": reason}


async def score_completeness(
    db: Session,
    task_id: str,
    *,
    design: ImplDesign | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the six mechanical completeness checks and return their reasons."""

    task = db.get(Task, task_id)
    if task is None:
        raise ImplDesignError(f"Task {task_id} not found")
    design = design or db.query(ImplDesign).filter(ImplDesign.task_id == task_id).first()
    if design is None:
        raise ImplDesignError(f"Task {task_id} has no impl_design")

    project = db.get(Project, task.project) if task.project else None
    repo_root = project.repo_root if project else None
    file_failures: list[str] = []
    if not isinstance(design.files, list):
        file_failures.append("files must be an array")
    for index, entry in enumerate(design.files if isinstance(design.files, list) else []):
        if not isinstance(entry, Mapping):
            file_failures.append(f"files[{index}] is not an object with path/action")
            continue
        path = str(entry.get("path") or "").strip()
        action = str(entry.get("action") or "").strip().lower()
        if not path:
            file_failures.append(f"files[{index}] has no path")
            continue
        if action not in {"create", "modify", "delete"}:
            file_failures.append(f"file '{path}' has invalid action '{action or '<missing>'}'")
            continue
        if action == "create":
            continue
        candidate, path_error = _repo_path(repo_root, path)
        if path_error:
            file_failures.append(path_error)
        elif candidate is not None and not candidate.is_file():
            file_failures.append(f"file '{path}' does not exist in the repository")
    file_check = _check(
        "file_paths",
        not file_failures,
        "all referenced files exist or are explicitly marked action='create'"
        if not file_failures else "; ".join(file_failures),
    )

    changes = design.changes if isinstance(design.changes, list) else []
    symbol_failures: list[str] = []
    if not isinstance(design.changes, list):
        symbol_failures.append("changes must be an array")
    for index, entry in enumerate(changes):
        if not isinstance(entry, Mapping):
            symbol_failures.append(f"changes[{index}] is not an object with symbol")
            continue
        symbol = str(entry.get("symbol") or "").strip()
        if not symbol:
            symbol_failures.append(f"changes[{index}] has no symbol")
            continue
        if _is_new_symbol(entry):
            continue
        if not await _symbol_exists(repo_root, symbol):
            symbol_failures.append(f"symbol '{symbol}' was not found in the code graph")
    symbol_check = _check(
        "symbols",
        not symbol_failures,
        "all referenced symbols exist in the code graph or are marked new"
        if not symbol_failures else "; ".join(symbol_failures),
    )

    test_plan_check = _check(
        "test_plan",
        _nonempty(design.test_plan),
        "test_plan is present" if _nonempty(design.test_plan) else "test_plan is empty",
    )
    non_goals_check = _check(
        "non_goals",
        _nonempty(design.non_goals),
        "non_goals is present" if _nonempty(design.non_goals) else "non_goals is empty",
    )

    change_failures: list[str] = []
    if not isinstance(design.changes, list):
        change_failures.append("changes must be an array")
    else:
        for index, entry in enumerate(design.changes):
            if not isinstance(entry, Mapping):
                change_failures.append(f"changes[{index}] must be an object")
                continue
            symbol = str(entry.get("symbol") or "").strip()
            behavior = entry.get("behavior")
            if not symbol:
                change_failures.append(f"changes[{index}] is missing symbol")
            if not _nonempty(behavior):
                change_failures.append(f"changes[{index}] ({symbol or '<unknown>'}) is missing behavior")
    change_check = _check(
        "change_entries",
        not change_failures,
        "every change has a symbol and behavior"
        if not change_failures else "; ".join(change_failures),
    )

    head_sha = _git_head(repo_root)
    design_sha = (design.derived_from_sha or "").strip()
    if not design_sha:
        sha_reason = f"derived_from_sha is missing; current HEAD is {head_sha or '<unavailable>'}"
        sha_passed = False
    elif not head_sha:
        sha_reason = f"cannot verify derived_from_sha '{design_sha}': repository HEAD is unavailable"
        sha_passed = False
    elif design_sha != head_sha:
        sha_reason = f"design is based on {design_sha}, but current HEAD is {head_sha}"
        sha_passed = False
    else:
        sha_reason = f"derived_from_sha matches current HEAD {head_sha}"
        sha_passed = True
    sha_check = _check("derived_from_sha", sha_passed, sha_reason)

    checks = [file_check, symbol_check, test_plan_check, non_goals_check, change_check, sha_check]
    result = {
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "head_sha": head_sha,
    }
    if persist:
        design.completeness = result
        db.flush()
    return result


def _serialize(design: ImplDesign) -> dict[str, Any]:
    return {
        "id": design.id,
        "task_id": design.task_id,
        "summary": design.summary,
        "files": design.files or [],
        "changes": design.changes or [],
        "data_changes": design.data_changes or [],
        "test_plan": design.test_plan or [],
        "risks": design.risks or [],
        "non_goals": design.non_goals or [],
        "derived_from_sha": design.derived_from_sha,
        "authored_by": design.authored_by,
        "completeness": design.completeness,
        "reviewed_by": design.reviewed_by,
    }


async def save_design(db: Session, task_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create or replace the current design, then immediately score it."""

    task = db.get(Task, task_id)
    if task is None:
        raise ImplDesignError(f"Task {task_id} not found")
    required = ("summary", "files", "changes", "data_changes", "test_plan", "risks", "non_goals", "derived_from_sha", "authored_by")
    missing = [key for key in required if key not in payload]
    if missing:
        raise ImplDesignError(f"impl_design is missing required fields: {', '.join(missing)}")

    design = db.query(ImplDesign).filter(ImplDesign.task_id == task_id).first()
    if design is None:
        design = ImplDesign(task_id=task_id)
        db.add(design)
    for field in (
        "summary", "files", "changes", "data_changes", "test_plan", "risks",
        "non_goals", "derived_from_sha", "authored_by", "reviewed_by",
    ):
        if field in payload:
            setattr(design, field, payload[field])
    db.flush()
    await score_completeness(db, task_id, design=design, persist=True)
    db.commit()
    db.refresh(design)
    return _serialize(design)


async def get_design(db: Session, task_id: str) -> dict[str, Any]:
    design = db.query(ImplDesign).filter(ImplDesign.task_id == task_id).first()
    if design is None:
        raise ImplDesignError(f"Task {task_id} has no impl_design")
    return _serialize(design)


__all__ = ["CHECK_NAMES", "ImplDesignError", "get_design", "save_design", "score_completeness"]
