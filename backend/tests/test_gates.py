import pytest
from app.graph.state import (
    TaskState,
    GateType,
    Mode,
    FourEyesViolation,
    SpecGateError,
    DispatchGateError,
    ReviewGateError,
    VerdictGateError
)
from app.graph.gates import (
    spec_gate,
    plan_gate,
    dispatch_gate,
    review_gate,
    verdict_gate,
    parse_raw_input
)


def test_parse_raw_input():
    project, title = parse_raw_input("/pm add dark mode --project web")
    assert project == "web"
    assert "dark mode" in title

    project, title = parse_raw_input("[mobile] Fix crash on login")
    assert project == "mobile"
    assert title == "Fix crash on login"

    project, title = parse_raw_input("backend: Optimize database query")
    assert project == "backend"
    assert title == "Optimize database query"


def test_spec_gate_success():
    state = TaskState(raw_input="/pm add dark mode --project web")
    result = spec_gate(state)

    assert result.project == "web"
    assert result.title is not None
    assert len(result.acceptance_criteria) > 0
    assert result.risk in ["low", "medium", "high"]
    assert result.current_gate == GateType.SPEC
    assert len(result.audit_trail) >= 1


def test_spec_gate_invalid_input():
    state = TaskState(raw_input="")
    with pytest.raises(SpecGateError):
        spec_gate(state)


def test_plan_gate():
    state = TaskState(
        task_id="CTV2-004",
        title="Gate Implementations",
        acceptance_criteria=["AC1", "AC2"],
        files=["backend/app/graph/gates/spec.py"],
        tests=["backend/tests/test_gates.py"]
    )
    result = plan_gate(state)

    assert result.plan is not None
    assert len(result.plan) > 0
    assert result.current_gate == GateType.PLAN
    assert len(result.audit_trail) >= 1


def test_dispatch_gate_success():
    state = TaskState(
        task_id="CTV2-004",
        title="Gate Implementations",
        executor="@antigravity-3.6-high"
    )
    result = dispatch_gate(state)

    assert result.executor == "@antigravity-3.6-high"
    assert result.status == "dispatched"
    assert result.dispatched_at is not None
    assert result.current_gate == GateType.DISPATCH


def test_dispatch_gate_default_executor():
    state = TaskState(task_id="CTV2-004", executor=None)
    result = dispatch_gate(state)

    assert result.executor is not None
    assert result.status == "dispatched"


def test_review_gate_success():
    state = TaskState(
        task_id="CTV2-004",
        executor="@alice",
        result_ref="commit:abc1234"
    )
    result = review_gate(state)

    assert result.result_ref == "commit:abc1234"
    assert result.status == "in-review"
    assert result.reviewer is not None
    assert result.reviewer != state.executor
    assert result.current_gate == GateType.REVIEW_ORDER


def test_review_gate_missing_result_ref():
    state = TaskState(task_id="CTV2-004", executor="@alice", result_ref=None)
    with pytest.raises(ReviewGateError):
        review_gate(state)


def test_verdict_gate_pass():
    state = TaskState(
        task_id="CTV2-004",
        executor="@alice",
        reviewer="@bob",
        verdict="pass"
    )
    result = verdict_gate(state)

    assert result.status == "done"
    assert result.completed_at is not None
    assert result.predicted_success is not None
    assert result.prediction_factors is not None


def test_verdict_gate_changes():
    state = TaskState(
        task_id="CTV2-004",
        executor="@alice",
        reviewer="@bob",
        verdict="changes",
        findings=["Needs test for edge case"]
    )
    result = verdict_gate(state)

    assert result.status == "changes-requested"
    assert len(result.findings) == 1
    assert result.findings[0] == "Needs test for edge case"


def test_four_eyes_violation():
    state = TaskState(
        task_id="CTV2-004",
        executor="@alice",
        reviewer="@alice",
        verdict="pass"
    )
    with pytest.raises(FourEyesViolation):
        verdict_gate(state)


def test_gate_bypass_mode():
    state = TaskState(
        raw_input="/pm add feature --project web",
        mode=Mode.BYPASS
    )
    result = spec_gate(state)
    assert result.awaiting_approval is False


def test_full_flow_integration():
    # 1. Spec Gate
    state = TaskState(raw_input="/pm add feature X --project core", mode=Mode.BYPASS)
    state = spec_gate(state)
    assert state.project == "core"
    assert len(state.acceptance_criteria) > 0

    # 2. Plan Gate
    state = plan_gate(state)
    assert state.plan is not None

    # 3. Dispatch Gate
    state.executor = "@dev1"
    state = dispatch_gate(state)
    assert state.status == "dispatched"

    # 4. Review Gate
    state.result_ref = "pr:42"
    state.reviewer = "@dev2"
    state = review_gate(state)
    assert state.status == "in-review"

    # 5. Verdict Gate
    state.verdict = "pass"
    state = verdict_gate(state)
    assert state.status == "done"
    assert state.completed_at is not None
    assert len(state.audit_trail) >= 5
