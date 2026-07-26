from datetime import datetime, timezone
from app.graph.state import TaskState, GateType, FourEyesViolation, VerdictGateError
from app.graph.gates.base import add_audit_log, check_gate_approval


def calculate_prediction_metrics(state: TaskState) -> tuple[str, dict]:
    """Calculate predicted_success score & factors based on gate execution & findings."""
    base_score = 0.85
    deductions = []

    if state.findings:
        deduction = len(state.findings) * 0.1
        base_score -= deduction
        deductions.append(f"Review findings (-{deduction:.2f})")

    if state.risk == "high":
        base_score -= 0.15
        deductions.append("High risk task (-0.15)")
    elif state.risk == "medium":
        base_score -= 0.05
        deductions.append("Medium risk task (-0.05)")

    if base_score >= 0.8:
        predicted = "high"
    elif base_score >= 0.5:
        predicted = "medium"
    else:
        predicted = "low"

    factors = {
        "score": round(base_score, 2),
        "deductions": deductions
    }
    return predicted, factors


def verdict_gate(state: TaskState) -> TaskState:
    """
    Verdict Gate Implementation:
    - Enforces four-eyes rule: reviewer != executor (raises FourEyesViolation if violated).
    - Checks verdict ('pass' or 'changes').
    - 'pass' -> status = 'done', completed_at populated.
    - 'changes' -> status = 'changes-requested', findings populated.
    - Updates prediction metrics.
    - Adds audit log and checks approval.
    """
    state.current_gate = GateType.VERDICT

    # Four-eyes enforcement (CRITICAL HARD FAIL)
    if state.executor and state.reviewer and state.executor == state.reviewer:
        raise FourEyesViolation(
            f"Four-eyes violation: reviewer '{state.reviewer}' cannot be the same as executor '{state.executor}'."
        )

    verdict_val = state.verdict or "pass"

    if verdict_val == "pass":
        state.status = "done"
        state.completed_at = datetime.now(timezone.utc).isoformat()
    elif verdict_val == "changes":
        state.status = "changes-requested"
        if not state.findings:
            state.findings = ["Changes requested by reviewer during verdict evaluation."]
    else:
        raise VerdictGateError(f"Invalid verdict value: '{verdict_val}'. Expected 'pass' or 'changes'.")

    predicted_success, prediction_factors = calculate_prediction_metrics(state)
    state.predicted_success = predicted_success
    state.prediction_factors = prediction_factors

    add_audit_log(state, f"gate:verdict:{verdict_val}", {
        "verdict": verdict_val,
        "status": state.status,
        "completed_at": state.completed_at,
        "findings_count": len(state.findings),
        "predicted_success": state.predicted_success
    })

    check_gate_approval(state, "verdict")
    return state
