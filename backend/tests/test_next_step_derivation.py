"""`next` must be derived from the whole state, not one column (CTV2-1404).

Three very different situations all read `status == 'todo'`: nothing started,
a planner running, a gate pending. Answering "call generate_spec_plan" to all
three sent a coordinator to start work already underway.
"""

from __future__ import annotations

from app.mcp_native import _next_step


def _task(**kw):
    base = {"status": "todo", "current_gate": None, "awaiting_approval": False}
    base.update(kw)
    return {"task": base}


def test_planner_running_says_wait_not_generate():
    step = _next_step(_task(status="todo", current_gate="plan"))
    assert "wait_for_task" in step
    assert "generate_spec_plan" in step  # named only to say: do NOT call it again
    assert "trùng" in step


def test_spec_gate_also_counts_as_planning():
    assert "wait_for_task" in _next_step(_task(status="todo", current_gate="spec"))


def test_fresh_todo_still_says_generate_plan():
    step = _next_step(_task(status="todo", current_gate=None))
    assert "generate_spec_plan" in step
    assert "wait_for_task" not in step


def test_pending_gate_outranks_status():
    """Every transition tool refuses while a gate is pending; say so."""
    step = _next_step(_task(status="todo", current_gate="plan", awaiting_approval=True))
    assert "approve_gate" in step
    assert "wait_for_task" not in step


def test_pending_gate_teaches_the_authority():
    step = _next_step(_task(status="dispatched", current_gate="execution", awaiting_approval=True))
    assert "không phải chỗ xin phép" in step


def test_terminal_task_is_not_treated_as_waiting():
    step = _next_step(_task(status="done", awaiting_approval=True))
    assert "done" in step
    assert "approve_gate" not in step


def test_failed_task_points_at_reopen():
    assert "reopen_task" in _next_step(_task(status="failed"))


def test_no_task_returns_none():
    assert _next_step({"action": "nothing"}) is None
