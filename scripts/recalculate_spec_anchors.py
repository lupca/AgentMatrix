#!/usr/bin/env python3
"""Recalculate living-spec anchor hashes from each project's checked-out repo.

This is a one-off data repair for anchors written with a commit SHA instead of
the hash of the anchored symbol.  It never deletes anchors.  Anchors whose
file or symbol cannot be resolved are reported and left unchanged.
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
from app.services.spec_anchor import compute_anchor_sha

_ANCHOR_SHA_RE = re.compile(r"[0-9a-fA-F]{64}")


def recalculate(*, dry_run: bool = False) -> dict:
    """Recompute every anchor using the repo root configured on its project."""
    db = SessionLocal()
    legacy_candidates = 0
    updated = 0
    recomputed_matches = 0
    all_computable = 0
    valid_anchor_drift = 0
    uncomputable: list[dict[str, str | None]] = []
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
                if is_legacy_anchor:
                    uncomputable.append({
                        "anchor_id": anchor.id,
                        "project_id": project_id,
                        "repo_root": repo_root,
                        "path": anchor.path,
                        "symbol": anchor.symbol,
                        "stored_anchor_sha": anchor.anchor_sha,
                    })
                continue

            all_computable += 1
            if not is_legacy_anchor:
                if current_sha != anchor.anchor_sha:
                    valid_anchor_drift += 1
                continue

            recomputed_matches += 1
            if current_sha != anchor.anchor_sha:
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
        "recomputed_matches": recomputed_matches,
        "matched_after_recalculation": recomputed_matches,
        "updated": updated,
        "uncomputable": len(uncomputable),
        "uncomputable_anchors": uncomputable,
        "all_anchors_computable": all_computable,
        "valid_anchor_drift_preserved": valid_anchor_drift,
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
