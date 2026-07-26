from app.graph.state import TaskState, GateType
from app.graph.gates.base import add_audit_log, check_gate_approval
from app.services.llm import LLMClient
from app.core.compression import compress_file_list, compress_test_list


def plan_gate(state: TaskState) -> TaskState:
    """
    Plan Gate Implementation:
    - Calls LLM to generate implementation plan based on title, AC, files, tests.
    - Sets state.plan.
    - Adds audit log and checks approval.
    - Uses Headroom compression for large file/test lists when enabled.
    """
    state.current_gate = GateType.PLAN

    plan_text = ""

    try:
        llm = LLMClient()
        files_str = compress_file_list(state.files) if state.files else "None specified"
        tests_str = compress_test_list(state.tests) if state.tests else "None specified"

        prompt = (
            f"You are a technical lead creating an implementation plan.\n"
            f"Title: {state.title or 'Untitled Task'}\n"
            f"Acceptance Criteria:\n" + "\n".join(f"- {ac}" for ac in state.acceptance_criteria) + "\n"
            f"Target Files: {files_str}\n"
            f"Target Tests: {tests_str}\n\n"
            f"Write a concise step-by-step implementation plan."
        )
        plan_text = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.5,
            operation="plan",
            task_id=state.task_id,
        )
    except Exception:
        plan_text = _generate_fallback_plan(state)

    state.plan = plan_text

    add_audit_log(state, "gate:plan:pass", {
        "plan_length": len(plan_text)
    })

    check_gate_approval(state, "plan")
    return state


def _generate_fallback_plan(state: TaskState) -> str:
    lines = [
        f"# Implementation Plan: {state.title or 'Task'}",
        "## Steps:",
        "1. Setup module environment and dependencies",
    ]
    if state.acceptance_criteria:
        for idx, ac in enumerate(state.acceptance_criteria, start=2):
            lines.append(f"{idx}. Implement logic for: {ac}")
    else:
        lines.append("2. Implement core functionality")

    if state.files:
        lines.append("## Files to edit:")
        for f in state.files:
            lines.append(f"- {f}")

    if state.tests:
        lines.append("## Tests to run:")
        for t in state.tests:
            lines.append(f"- {t}")

    return "\n".join(lines)
