import os
from app.graph.state import TaskState, GateType
from app.graph.gates.base import add_audit_log, check_gate_approval

try:
    import anthropic
except ImportError:
    anthropic = None


def plan_gate(state: TaskState) -> TaskState:
    """
    Plan Gate Implementation:
    - Calls Claude API (Sonnet) to generate implementation plan based on title, AC, files, tests.
    - Sets state.plan.
    - Adds audit log and checks approval.
    """
    state.current_gate = GateType.PLAN

    api_key = os.getenv("ANTHROPIC_API_KEY")
    plan_text = ""

    if api_key and anthropic:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            prompt = (
                f"You are a technical lead creating an implementation plan.\n"
                f"Title: {state.title or 'Untitled Task'}\n"
                f"Acceptance Criteria:\n" + "\n".join(f"- {ac}" for ac in state.acceptance_criteria) + "\n"
                f"Target Files: {', '.join(state.files) if state.files else 'None specified'}\n"
                f"Target Tests: {', '.join(state.tests) if state.tests else 'None specified'}\n\n"
                f"Write a concise step-by-step implementation plan."
            )
            response = client.messages.create(
                model="claude-3-5-sonnet-latest",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}]
            )
            plan_text = response.content[0].text
        except Exception:
            plan_text = _generate_fallback_plan(state)
    else:
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
