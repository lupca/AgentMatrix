import os
import re
import json
from typing import Tuple
from app.graph.state import TaskState, GateType, SpecGateError
from app.graph.gates.base import add_audit_log, check_gate_approval
from app.services.llm import LLMClient


def parse_raw_input(raw_input: str) -> Tuple[str | None, str | None]:
    """Extract project and title from raw_input."""
    if not raw_input or not raw_input.strip():
        return None, None

    text = raw_input.strip()

    # Pattern 1: --project <name> or -p <name>
    project_match = re.search(r'(?:--project|-p)\s+([a-zA-Z0-9_-]+)', text)
    project = project_match.group(1) if project_match else None

    # Remove project flag from title if present
    cleaned = re.sub(r'(?:--project|-p)\s+[a-zA-Z0-9_-]+', '', text).strip()

    # Remove slash commands like /pm add, /pm, /task
    cleaned = re.sub(r'^/(?:pm|task)\s+(?:add\s+)?', '', cleaned, flags=re.IGNORECASE).strip()

    # Pattern 2: [project] Title
    if not project:
        bracket_match = re.match(r'^\[([a-zA-Z0-9_-]+)\]\s*(.+)$', cleaned)
        if bracket_match:
            project = bracket_match.group(1)
            cleaned = bracket_match.group(2)

    # Pattern 3: project: Title
    if not project:
        colon_match = re.match(r'^([a-zA-Z0-9_-]+):\s*(.+)$', cleaned)
        if colon_match:
            project = colon_match.group(1)
            cleaned = colon_match.group(2)

    title = cleaned.strip() if cleaned else None
    return project, title


def spec_gate(state: TaskState) -> TaskState:
    """
    Spec Gate Implementation:
    - Parses raw input to extract project, title.
    - Calls Claude API (Haiku) to validate spec & generate AC.
    - Updates acceptance_criteria and risk assessment.
    - Adds audit log and checks approval.
    """
    if not state.raw_input and not state.title:
        raise SpecGateError("Raw input or title is required for Spec Gate.")

    project, title = parse_raw_input(state.raw_input)
    
    if project:
        state.project = project
    elif not state.project:
        state.project = "default"

    if title:
        state.title = title
    elif not state.title:
        raise SpecGateError("Could not parse a valid title from input.")

    state.current_gate = GateType.SPEC

    ac_list = []
    risk = "medium"

    try:
        llm = LLMClient()
        prompt = (
            f"You are a software spec validator.\n"
            f"Task Title: {state.title}\n"
            f"Project: {state.project}\n\n"
            f"Respond with a valid JSON object only containing two keys:\n"
            f'  "acceptance_criteria": list of 2-4 specific testable string criteria\n'
            f'  "risk": one of "low", "medium", "high"\n'
        )
        content = llm.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
            operation="spec",
            task_id=state.task_id,
        )
        # Clean possible markdown block
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        parsed = json.loads(content.strip())
        ac_list = parsed.get("acceptance_criteria", [])
        risk = parsed.get("risk", "medium")
    except Exception:
        ac_list = [
            f"Implement feature: {state.title}",
            f"Add unit tests for {state.title}",
            "Ensure no regressions in project"
        ]
        risk = "medium"

    state.acceptance_criteria = ac_list
    state.risk = risk

    add_audit_log(state, "gate:spec:pass", {
        "title": state.title,
        "project": state.project,
        "ac_count": len(ac_list),
        "risk": risk
    })

    check_gate_approval(state, "spec")
    return state
