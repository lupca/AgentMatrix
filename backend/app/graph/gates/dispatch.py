from datetime import datetime, timezone
from app.graph.state import TaskState, GateType, DispatchGateError
from app.graph.gates.base import add_audit_log, check_gate_approval

DEFAULT_EXECUTOR = "@antigravity-3.6-high"


def generate_dispatch_command(task_id: str, executor: str, project: str | None = None) -> str:
    """Generate dispatch command string (reusing ct-dispatch logic format)."""
    cmd = f"python scripts/ct-dispatch.py --task {task_id} --executor {executor}"
    if project:
        cmd += f" --project {project}"
    return cmd


def dispatch_gate(state: TaskState) -> TaskState:
    """
    Dispatch Gate Implementation:
    - Assigns executor from state or default.
    - Fails with DispatchGateError if executor is invalid/empty and cannot be assigned.
    - Generates dispatch command.
    - Updates status -> 'dispatched' and dispatched_at.
    - Adds audit log and checks approval.
    """
    state.current_gate = GateType.DISPATCH

    executor = state.executor or DEFAULT_EXECUTOR
    if not executor or not executor.strip():
        raise DispatchGateError("No executor assigned for Dispatch Gate.")

    state.executor = executor.strip()
    state.status = "dispatched"
    state.dispatched_at = datetime.now(timezone.utc).isoformat()

    dispatch_cmd = generate_dispatch_command(
        task_id=state.task_id or "CTV2-TASK",
        executor=state.executor,
        project=state.project
    )

    add_audit_log(state, "gate:dispatch:pass", {
        "executor": state.executor,
        "dispatched_at": state.dispatched_at,
        "dispatch_command": dispatch_cmd
    })

    check_gate_approval(state, "dispatch")
    return state
