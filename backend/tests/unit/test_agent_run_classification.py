from types import SimpleNamespace

import pytest
from app.services.agent_run_classification import (
    classify_failure,
    classify_review_outcome,
    failure_report,
)


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        ("failed", "timeout waiting for response", "infra_timeout"),
        ("failed", "qwen: unknown option --yolo", "infra_config"),
        ("failed", "qwen: tool write_file not available", "infra_config"),
        ("failed", "idempotency key already exists for task", "infra_conflict"),
        ("failed", "invalid JSON in review result file", "infra_parse"),
        ("failed", "Agent completed without committed changes", "agent_no_output"),
        ("cancelled", "Cancelled by user", "cancelled"),
    ],
)
def test_known_termination_signals_are_classified(status, error, expected):
    assert classify_failure(status=status, error=error) == expected


def test_ambiguous_exit_is_unknown_instead_of_agent_failure():
    assert classify_failure(status="failed", error="Exit code: 2") == "unknown"


def test_failed_acceptance_criterion_is_agent_incomplete():
    assert classify_review_outcome([{"status": "pass"}, {"status": "fail"}]) == "agent_incomplete"
    assert classify_review_outcome([{"status": "pass"}]) == "unknown"


def test_failure_report_exposes_contamination_and_excludes_legacy_from_clean_ratio():
    runs = [
        SimpleNamespace(
            status="failed", failure_category="infra_timeout", failure_data_quality="current"
        ),
        SimpleNamespace(
            status="failed", failure_category="agent_no_output", failure_data_quality="current"
        ),
        SimpleNamespace(
            status="failed", failure_category="unknown", failure_data_quality="legacy"
        ),
        SimpleNamespace(
            status="success", failure_category="unknown", failure_data_quality="current"
        ),
    ]

    report = failure_report(runs)

    assert report["failed_runs_total"] == 3
    assert report["classified_failed_runs"] == 2
    assert report["legacy_failed_runs"] == 1
    assert report["unknown_failed_runs"] == 1
    assert report["infra_failed_runs"] == 1
    assert report["agent_failed_runs"] == 1
    assert report["infra_vs_agent"] == {
        "infra": 1,
        "agent": 1,
        "ratio": 1.0,
        "infra_share": 0.5,
    }
