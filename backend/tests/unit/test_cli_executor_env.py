from app.workers.cli_executor import _process_env_for_cli


def test_qwen_process_env_suppresses_yolo_warning_and_preserves_mcp_env():
    process_env, review_git_dir = _process_env_for_cli(
        "qwen", {"CT_MCP_TOKEN": "secret"}, is_review_run=False
    )

    assert process_env == {
        "CT_MCP_TOKEN": "secret",
        "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
    }
    assert review_git_dir is None


def test_non_qwen_process_env_does_not_receive_qwen_warning_setting():
    process_env, review_git_dir = _process_env_for_cli(
        "claude", {"CT_MCP_TOKEN": "secret"}, is_review_run=False
    )

    assert process_env == {"CT_MCP_TOKEN": "secret"}
    assert review_git_dir is None
