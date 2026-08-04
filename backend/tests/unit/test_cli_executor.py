import os
from unittest.mock import MagicMock

from app.workers import cli_executor
from app.workers.output_parser import ReviewResultLoadError


def test_record_review_result_load_failure_persists_structured_errors(monkeypatch):
    metric = MagicMock()
    monkeypatch.setattr(cli_executor, "record_tool_metric", metric)
    orchestration = MagicMock()
    run = MagicMock(id="review-run", agent_id="@claude-opus")
    error_details = {
        "errors": [
            {
                "type": "extra_forbidden",
                "loc": ["toolchain_notes"],
                "msg": "Extra inputs are not permitted",
                "input": "notes",
            }
        ]
    }
    exc = ReviewResultLoadError(
        "schema_validation",
        "/repo/.ct/review-TASK.json",
        "Review result does not match its schema",
        **error_details,
    )

    status = cli_executor._record_review_result_load_failure(
        MagicMock(), run, "TASK", exc, orchestration
    )

    assert status == "failed"
    assert run.status == "failed"
    metric.assert_called_once_with(
        tool="review_result",
        source="agent_runner",
        ok=False,
        task_id="TASK",
        error="schema_validation: Review result does not match its schema",
        payload=exc.as_dict(),
    )
    orchestration.return_value.record_review_failure.assert_called_once_with(
        task_id="TASK",
        error="Review result does not match its schema",
        actor="agent:@claude-opus",
        idempotency_key="run:review-run:review-result-invalid",
        run_id="review-run",
        error_details=exc.as_dict(),
    )


def test_prepare_review_artifact_generates_template(tmp_path):
    """Template JSON should have exact AC count."""
    from app.workers.cli_executor import _prepare_review_artifact
    from app.services.command_builder import review_result_path
    import json

    repo_root = str(tmp_path)
    task_id = "TEST-001"
    acceptance_criteria = ["AC 1", "AC 2", "AC 3"]

    _prepare_review_artifact(repo_root, task_id, acceptance_criteria)

    template_path = review_result_path(repo_root, task_id).replace(".json", ".template.json")
    assert os.path.exists(template_path)

    with open(template_path) as f:
        template = json.load(f)

    assert template["task_id"] == task_id
    assert len(template["ac_results"]) == 3
    assert template["ac_results"][0]["criterion_id"] == "ac-1"
    assert template["ac_results"][2]["criterion_id"] == "ac-3"


def test_prepare_review_artifact_appends_constraints_to_review_contract(tmp_path):
    from app.workers.cli_executor import _prepare_review_artifact
    from app.services.command_builder import review_result_path
    import json

    repo_root = str(tmp_path)
    _prepare_review_artifact(
        repo_root,
        "TEST-CONSTRAINTS",
        ["Endpoint returns 200"],
        ["Do not add a migration", "Preserve stream-json"],
    )
    template_path = review_result_path(repo_root, "TEST-CONSTRAINTS").replace(
        ".json", ".template.json"
    )
    with open(template_path) as result_file:
        template = json.load(result_file)
    assert [item["criterion_id"] for item in template["ac_results"]] == [
        "ac-1", "ac-2", "ac-3"
    ]


def test_prepare_review_artifact_no_template_without_ac(tmp_path):
    """No template if no acceptance criteria."""
    from app.workers.cli_executor import _prepare_review_artifact
    from app.services.command_builder import review_result_path

    repo_root = str(tmp_path)
    task_id = "TEST-002"

    _prepare_review_artifact(repo_root, task_id, None)

    template_path = review_result_path(repo_root, task_id).replace(".json", ".template.json")
    assert not os.path.exists(template_path)


def test_prepare_review_artifact_handles_string_ac(tmp_path):
    """Template should parse string AC format correctly."""
    from app.workers.cli_executor import _prepare_review_artifact
    from app.services.command_builder import review_result_path
    import json

    repo_root = str(tmp_path)
    task_id = "TEST-003"
    # String format like VOMA tasks have
    string_ac = "AC1: First criterion\nAC2: Second criterion\nAC3: Third"

    _prepare_review_artifact(repo_root, task_id, string_ac)

    template_path = review_result_path(repo_root, task_id).replace(".json", ".template.json")
    assert os.path.exists(template_path)

    with open(template_path) as f:
        template = json.load(f)

    assert len(template["ac_results"]) == 3  # Not 48 (string length)
