"""Build the single flat contract that code reviewers must check."""

from __future__ import annotations


def normalize_criteria(value: list | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [line.strip() for line in value.split("\n") if line.strip()]


def merged_review_criteria(
    acceptance_criteria: list | str | None,
    constraints: list | str | None,
) -> list[str]:
    """Acceptance first, then negative boundaries, preserving stable IDs."""

    return normalize_criteria(acceptance_criteria) + normalize_criteria(constraints)
