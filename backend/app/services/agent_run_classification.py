"""Conservative, vendor-independent AgentRun termination classification."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

UNKNOWN = "unknown"
LEGACY_DATA = "legacy"
CURRENT_DATA = "current"
LEGACY_CUTOFF = "2026-08-04T00:00:00+00:00"

FAILURE_CATEGORIES = (
    "infra_timeout",
    "infra_config",
    "infra_conflict",
    "infra_parse",
    "agent_no_output",
    "agent_wrong",
    "agent_incomplete",
    "brake_stopped",
    "cancelled",
    UNKNOWN,
)
INFRA_CATEGORIES = frozenset(
    category for category in FAILURE_CATEGORIES if category.startswith("infra_")
)
AGENT_CATEGORIES = frozenset(
    category for category in FAILURE_CATEGORIES if category.startswith("agent_")
)


def _message(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


NON_RETRYABLE_MODEL_PATTERNS = (
    "invalid model",
    "model not found",
    "unknown model",
    "model does not exist",
    "unsupported model",
    "no such model",
    "invalid model name",
    "valid models",
    "available models",
)

NON_RETRYABLE_EXECUTABLE_PATTERNS = (
    "executable not found",
    "command not found",
    "cli binary not found",
    "binary not found",
    "not found in path",
    "no such file or directory",
)


def is_non_retryable_error(*texts: str | None) -> bool:
    """Check if output/error matches non-retryable (permanent) failure patterns.

    Non-retryable failures:
    - Model does not exist / invalid model
    - HTTP 400 errors involving model
    - Executable / CLI binary not found
    """
    combined = _message(" ".join(t for t in texts if t))
    if not combined:
        return False

    if any(marker in combined for marker in NON_RETRYABLE_MODEL_PATTERNS):
        return True

    if any(marker in combined for marker in NON_RETRYABLE_EXECUTABLE_PATTERNS):
        return True

    if "400" in combined and "model" in combined:
        return True

    return False


def format_non_retryable_error_message(error: str | None, raw_output: str | None = None) -> str:
    """Format a non-retryable error message explaining the reason and fix action."""
    detail = (error or "").strip() or (raw_output or "").strip() or "Process failed with non-retryable error"
    detail_clean = re.sub(r"\s+", " ", detail)
    return (
        f"Non-retryable execution error: {detail_clean}. "
        "Fix: please update the model or agent configuration."
    )


def classify_termination(
    *,
    status: str | None,
    error: str | None = None,
    exit_code: int | None = None,
    output_lines: int | None = None,
    kind: str | None = None,
) -> str:
    """Classify only facts that are observable at run termination.

    The ordering is intentional: an explicit lifecycle status wins over a
    generic error string, and no fallback guesses whether an agent was wrong
    or incomplete.  Those labels require independent review evidence.
    """

    normalized_status = _message(status)
    message = _message(error)

    if normalized_status == "cancelled" or "cancelled by user" in message:
        return "cancelled"
    if normalized_status == "timeout":
        return "infra_timeout"
    if any(
        marker in message
        for marker in (
            "timeout waiting for response",
            "timed out waiting for response",
            "print-timeout",
            "timeout after ",
            "request timed out",
            "deadline exceeded",
        )
    ):
        return "infra_timeout"
    if any(
        marker in message
        for marker in (
            "idempotency key",
            "idempotency conflict",
            "already exists for task",
            "gate conflict",
            "worktree",
            "repository head",
            "integration checkout",
            "dead-letter",
        )
    ):
        return "infra_conflict"
    if any(
        marker in message
        for marker in (
            "invalid json",
            "malformed json",
            "could not parse",
            "parse error",
            "invalid review result",
            "review result file",
            "unexpected end of json",
            "jsondecodeerror",
        )
    ):
        return "infra_parse"
    if is_non_retryable_error(message) or any(
        marker in message
        for marker in (
            "unknown option",
            "unknown argument",
            "unrecognized argument",
            "unrecognized option",
            "no such option",
            "invalid option",
            "missing required tool",
        )
    ) or (
        "tool" in message
        and any(
            marker in message
            for marker in ("not found", "not available", "unavailable", "permission denied")
        )
    ):
        return "infra_config"
    if any(
        marker in message
        for marker in (
            "safety brake",
            "cost brake",
            "token brake",
            "budget brake",
            "rate limit brake",
        )
    ):
        return "brake_stopped"
    if any(
        marker in message
        for marker in (
            "without committed changes",
            "no committed changes",
            "declared 'result_ref: none'",
            "result_ref: none",
            "no output",
            "ended without a result",
        )
    ):
        return "agent_no_output"
    # A non-zero exit alone does not say whether the vendor, infrastructure,
    # or agent caused the failure.  Leave it unknown instead of polluting
    # capability measurements with a made-up attribution.
    return UNKNOWN


def classify_review_outcome(ac_results: Iterable[Any]) -> str:
    """Attribute an executor only when an independent review failed an AC."""

    for result in ac_results:
        status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
        if str(status or "").strip().lower() == "fail":
            # A failed acceptance criterion is direct evidence of an
            # incomplete result.  Do not infer the narrower "wrong" label
            # from a generic reviewer finding.
            return "agent_incomplete"
    return UNKNOWN


# Short name for callers and tests that talk about failure rather than the
# complete termination lifecycle.
classify_failure = classify_termination


def failure_report(runs: Iterable[Any]) -> dict[str, Any]:
    """Build a contamination report from the supplied AgentRun rows.

    Legacy rows are counted and exposed, but are not used for the clean
    classified ratio.  Cancellation is a terminal outcome, not a failed run.
    """

    rows = list(runs)
    failed = [
        run for run in rows if getattr(run, "status", None) in {"failed", "timeout"}
    ]
    categories = Counter(
        getattr(run, "failure_category", None) or UNKNOWN for run in failed
    )
    classified = [
        run
        for run in failed
        if (getattr(run, "failure_category", None) or UNKNOWN) != UNKNOWN
        and getattr(run, "failure_data_quality", CURRENT_DATA) != LEGACY_DATA
    ]
    infra = [
        run
        for run in classified
        if (getattr(run, "failure_category", None) or UNKNOWN) in INFRA_CATEGORIES
    ]
    agent = [
        run
        for run in classified
        if (getattr(run, "failure_category", None) or UNKNOWN) in AGENT_CATEGORIES
    ]
    denominator = len(infra) + len(agent)
    return {
        "failed_runs_total": len(failed),
        "status_failed_runs_total": sum(
            getattr(run, "status", None) == "failed" for run in rows
        ),
        "classified_failed_runs": len(classified),
        "unknown_failed_runs": categories[UNKNOWN],
        "legacy_failed_runs": sum(
            getattr(run, "failure_data_quality", CURRENT_DATA) == LEGACY_DATA
            for run in failed
        ),
        "by_category": dict(sorted(categories.items())),
        "infra_failed_runs": len(infra),
        "agent_failed_runs": len(agent),
        "infra_vs_agent": {
            "infra": len(infra),
            "agent": len(agent),
            "ratio": round(len(infra) / len(agent), 4) if agent else None,
            "infra_share": round(len(infra) / denominator, 4)
            if denominator
            else None,
        },
        "clean_sample_excludes": ["unknown", "legacy", "infra_*"],
    }
