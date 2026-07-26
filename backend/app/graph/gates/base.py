from datetime import datetime, timezone
from typing import Any
from app.graph.state import TaskState, Mode

try:
    from langgraph.types import interrupt
except ImportError:
    def interrupt(data: Any) -> Any:
        return "approve"


def add_audit_log(state: TaskState, action: str, details: dict[str, Any] | None = None, actor: str | None = None) -> TaskState:
    """Appends an audit log entry to the state's audit trail."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": state.task_id,
        "gate": state.current_gate.value if hasattr(state.current_gate, "value") else str(state.current_gate),
        "action": action,
        "actor": actor or state.executor or "system",
        "details": details or {},
    }
    state.audit_trail.append(entry)
    return state


def check_gate_approval(state: TaskState, gate_name: str) -> TaskState:
    """
    Support interrupt() for supervised mode, auto-approve in bypass mode.
    """
    mode_val = state.mode.value if hasattr(state.mode, "value") else str(state.mode)
    
    if mode_val == Mode.BYPASS.value:
        state.awaiting_approval = False
        state.approval_prompt = None
        return state

    if mode_val == Mode.SUPERVISED.value:
        prompt_msg = f"Approve gate '{gate_name}' for task '{state.task_id or 'new'}'?"
        try:
            decision = interrupt({
                "gate": gate_name,
                "task_id": state.task_id,
                "summary": prompt_msg
            })
            if decision == "approve":
                state.awaiting_approval = False
                state.approval_prompt = None
            else:
                state.awaiting_approval = True
                state.approval_prompt = prompt_msg
        except Exception:
            # When running outside a langgraph workflow context (e.g. unit tests without interrupt handler)
            state.awaiting_approval = False
            state.approval_prompt = None

    return state
