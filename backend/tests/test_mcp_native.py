from __future__ import annotations

from app.mcp_native import authenticate_token, envelope, issue_token


def test_role_token_round_trip_and_task_scope_claim():
    token = issue_token("secret", role="executor", task_id="task-1")
    claims = authenticate_token(token, secret="secret")
    assert claims is not None
    assert claims.role == "executor"
    assert claims.task_id == "task-1"
    assert authenticate_token(token, secret="wrong") is None


def test_unsigned_or_legacy_token_is_rejected():
    assert authenticate_token("legacy", secret="secret") is None


def test_native_envelope_structures_transition_error_and_hint():
    result = envelope({"error": "Task is already dispatched; expected status todo"})
    assert result["ok"] is False
    assert result["error"]["code"] == "task_transition_conflict"
    assert "hint" in result["error"]


def test_native_envelope_includes_next_for_task_state():
    result = envelope(
        {"task": {"id": "task-1", "status": "awaiting-review"}},
        next_step="Gọi request_review để bắt đầu review độc lập.",
    )
    assert result["ok"] is True
    assert result["next"] == "Gọi request_review để bắt đầu review độc lập."
