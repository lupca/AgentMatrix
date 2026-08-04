#!/usr/bin/env python3
"""Recalculate living-spec anchor hashes from each project's checked-out repo.

This is a one-off data repair for anchors written with a commit SHA instead of
the canonical content hash. Non-Python anchors are recalculated as whole-file
hashes. Python anchors are recalculated from local AST declarations; Python
anchors that do not identify a local declaration are deleted and reported.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Allow running the script from the repository root without installing the app.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.db.base import SessionLocal
from app.db.models import Project, SpecAnchor, SpecItem
from app.services.spec_anchor import compute_anchor_sha, is_python_path, source_available

_ANCHOR_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")


def recalculate(*, dry_run: bool = False) -> dict:
    """Recompute every anchor using the repo root configured on its project."""
    db = SessionLocal()
    legacy_candidates = 0
    updated = 0
    all_computable = 0
    valid_anchor_drift_recomputed = 0
    uncomputable: list[dict[str, str | None]] = []
    deleted_python: list[dict[str, str | None]] = []
    try:
        rows = (
            db.query(SpecAnchor, SpecItem.project_id, Project.repo_root)
            .join(SpecItem, SpecAnchor.spec_item_id == SpecItem.id)
            .join(Project, Project.id == SpecItem.project_id)
            .order_by(SpecAnchor.id)
            .all()
        )

        for anchor, project_id, repo_root in rows:
            is_legacy_anchor = not _ANCHOR_SHA_RE.fullmatch(anchor.anchor_sha)
            if is_legacy_anchor:
                legacy_candidates += 1
            current_sha = compute_anchor_sha(repo_root or "", anchor.path, anchor.symbol)
            if current_sha is None:
                detail = {
                    "anchor_id": anchor.id,
                    "project_id": project_id,
                    "repo_root": repo_root,
                    "path": anchor.path,
                    "symbol": anchor.symbol,
                    "stored_anchor_sha": anchor.anchor_sha,
                }
                if is_python_path(anchor.path) and source_available(repo_root or "", anchor.path):
                    deleted_python.append(detail)
                    if not dry_run:
                        db.delete(anchor)
                else:
                    uncomputable.append(detail)
                continue

            all_computable += 1
            if current_sha != anchor.anchor_sha:
                if not is_legacy_anchor:
                    valid_anchor_drift_recomputed += 1
                updated += 1
                if not dry_run:
                    anchor.anchor_sha = current_sha

        if not dry_run:
            db.flush()
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    return {
        "total": len(rows),
        "legacy_candidates": legacy_candidates,
        "updated": updated,
        "deleted_python": len(deleted_python),
        "deleted_python_anchors": deleted_python,
        "uncomputable": len(uncomputable),
        "uncomputable_anchors": uncomputable,
        "all_anchors_computable": all_computable,
        "valid_anchor_drift_recomputed": valid_anchor_drift_recomputed,
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report changes without writing recalculated hashes",
    )
    args = parser.parse_args()
    print(json.dumps(recalculate(dry_run=args.dry_run), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
