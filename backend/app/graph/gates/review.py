from app.graph.state import TaskState, GateType, ReviewGateError
from app.graph.gates.base import add_audit_log, check_gate_approval

DEFAULT_REVIEWER = "@antigravity-reviewer"


def generate_review_sheet(state: TaskState) -> str:
    """Generate review sheet text for reviewer."""
    lines = [
        f"# Review Sheet for Task: {state.task_id or 'Unknown'}",
        f"**Title**: {state.title or 'N/A'}",
        f"**Executor**: {state.executor or 'N/A'}",
        f"**Result Reference**: {state.result_ref}",
        "## Acceptance Criteria to Verify:",
    ]
    if state.acceptance_criteria:
        for idx, ac in enumerate(state.acceptance_criteria, start=1):
            lines.append(f"{idx}. [ ] {ac}")
    else:
        lines.append("- [ ] Verify code quality and test coverage")

    if state.files:
        lines.append("## Files Changed:")
        for f in state.files:
            lines.append(f"- {f}")

    return "\n".join(lines)


def review_gate(state: TaskState) -> TaskState:
    """
    Review-Order Gate Implementation:
    - Validates result_ref exists in state (raises ReviewGateError if missing).
    - Generates review sheet.
    - Assigns reviewer.
    - Updates status -> 'in-review'.
    - Adds audit log and checks approval.
    """
    state.current_gate = GateType.REVIEW_ORDER

    if not state.result_ref or not state.result_ref.strip():
        raise ReviewGateError("result_ref is required for Review Gate.")

    # Assign reviewer (prefer state.reviewer, else DEFAULT_REVIEWER)
    reviewer = state.reviewer or DEFAULT_REVIEWER
    if state.executor and reviewer == state.executor:
        reviewer = "@antigravity-peer" if state.executor != "@antigravity-peer" else "@antigravity-alt"

    state.reviewer = reviewer
    state.status = "in-review"

    review_sheet = generate_review_sheet(state)

    add_audit_log(state, "gate:review:pass", {
        "reviewer": state.reviewer,
        "result_ref": state.result_ref,
        "review_sheet_length": len(review_sheet)
    })

    check_gate_approval(state, "review_order")
    return state
