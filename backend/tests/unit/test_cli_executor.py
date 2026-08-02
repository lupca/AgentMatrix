import os


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


def test_prepare_review_artifact_no_template_without_ac(tmp_path):
    """No template if no acceptance criteria."""
    from app.workers.cli_executor import _prepare_review_artifact
    from app.services.command_builder import review_result_path

    repo_root = str(tmp_path)
    task_id = "TEST-002"

    _prepare_review_artifact(repo_root, task_id, None)

    template_path = review_result_path(repo_root, task_id).replace(".json", ".template.json")
    assert not os.path.exists(template_path)
