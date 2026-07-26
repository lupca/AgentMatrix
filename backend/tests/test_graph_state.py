import pytest
from app.graph.state import TaskState, GateType, Mode

def test_task_state_defaults():
    state = TaskState(raw_input="Test task input")
    assert state.raw_input == "Test task input"
    assert state.current_gate == GateType.SPEC
    assert state.status == "todo"
    assert state.mode == Mode.SUPERVISED
    assert state.acceptance_criteria == []
    assert state.files == []
    assert state.tests == []
    assert state.awaiting_approval is False

def test_task_state_gate_enum():
    assert GateType.SPEC.value == "spec"
    assert GateType.PLAN.value == "plan"
    assert GateType.DISPATCH.value == "dispatch"
    assert GateType.REVIEW_ORDER.value == "review_order"
    assert GateType.VERDICT.value == "verdict"

def test_task_state_mode_enum():
    assert Mode.PLAN_ONLY.value == "plan-only"
    assert Mode.SUPERVISED.value == "supervised"
    assert Mode.BYPASS.value == "bypass"

def test_task_state_serialization():
    state = TaskState(
        task_id="CTV2-003",
        title="LangGraph Core",
        raw_input="/pm CTV2-003 LangGraph Core",
        current_gate=GateType.PLAN,
        status="todo",
        executor="@antigravity",
        reviewer="@reviewer"
    )
    data = state.model_dump()
    assert data["task_id"] == "CTV2-003"
    assert data["title"] == "LangGraph Core"
    assert data["current_gate"] == "plan"
    
    restored = TaskState.model_validate(data)
    assert restored.task_id == "CTV2-003"
    assert restored.current_gate == GateType.PLAN
