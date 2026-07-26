from typing import Any, Dict, Union
import uuid
import logging
from app.graph.state import TaskState, GateType, Mode

logger = logging.getLogger(__name__)

def _get_state_obj(state: Union[TaskState, Dict[str, Any]]) -> TaskState:
    if isinstance(state, TaskState):
        return state
    return TaskState.model_validate(state)

def parse_input(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Parse raw input, extract or generate task metadata."""
    st = _get_state_obj(state)
    raw = st.raw_input.strip()
    
    task_id = st.task_id
    title = st.title
    project = st.project
    
    if raw.startswith("/pm "):
        parts = raw[4:].strip().split(" ", 1)
        if len(parts) == 2 and not task_id:
            # e.g. /pm test task or /pm TASK-1 Title
            if parts[0].isalnum() or "-" in parts[0]:
                task_id = task_id or parts[0]
                title = title or parts[1]
            else:
                title = title or raw[4:]
        else:
            title = title or raw[4:]
    elif not title and raw:
        title = raw
        
    if not task_id:
        task_id = f"TASK-{uuid.uuid4().hex[:6]}"
    if not project:
        project = "control-tower-v2"
        
    return {
        "task_id": task_id,
        "title": title,
        "project": project,
        "current_gate": GateType.SPEC,
        "status": st.status or "todo",
        "error": None
    }

def sync_to_db(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Sync graph state to database."""
    st = _get_state_obj(state)
    logger.info(f"[DB SYNC] Task {st.task_id} state updated: gate={st.current_gate}, status={st.status}")
    return {}

def log_action(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Log state action for audit trail."""
    st = _get_state_obj(state)
    logger.info(f"[AUDIT LOG] Action executed for task {st.task_id} at gate {st.current_gate}")
    return {}

def spec_gate(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Spec Gate node: Validate input and generate acceptance criteria."""
    st = _get_state_obj(state)
    ac = st.acceptance_criteria or ["AC1: Task defined", "AC2: Requirements parsed"]
    awaiting = (st.mode == Mode.SUPERVISED and not st.awaiting_approval)
    
    return {
        "current_gate": GateType.SPEC,
        "acceptance_criteria": ac,
        "awaiting_approval": awaiting,
        "approval_prompt": "Approve task spec?" if awaiting else None
    }

def approval(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Approval node: Process human-in-the-loop approval."""
    st = _get_state_obj(state)
    # Clear awaiting_approval upon resume/approval processing
    return {
        "awaiting_approval": False,
        "approval_prompt": None
    }

def plan_gate(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Plan Gate node: Generate implementation plan."""
    st = _get_state_obj(state)
    plan_text = st.plan or "1. Implement core features\n2. Add test coverage\n3. Verify results"
    return {
        "current_gate": GateType.PLAN,
        "plan": plan_text
    }

def dispatch_gate(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Dispatch Gate node: Assign executor and dispatch task."""
    st = _get_state_obj(state)
    return {
        "current_gate": GateType.DISPATCH,
        "status": "dispatched",
        "executor": st.executor or "@antigravity-3.6-high"
    }

def review_order_gate(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Review-Order Gate node: Assign reviewer and start review."""
    st = _get_state_obj(state)
    return {
        "current_gate": GateType.REVIEW_ORDER,
        "status": "in-review",
        "reviewer": st.reviewer or "@reviewer"
    }

def verdict_gate(state: Union[TaskState, Dict[str, Any]]) -> Dict[str, Any]:
    """Verdict Gate node: Evaluate review and enforce four-eyes rule."""
    st = _get_state_obj(state)
    # Enforce four-eyes rule: reviewer must differ from executor
    if st.executor and st.reviewer and st.executor == st.reviewer:
        return {
            "current_gate": GateType.VERDICT,
            "verdict": "changes",
            "status": "changes-requested",
            "error": "Four-eyes rule violation: executor and reviewer must be different"
        }
    
    return {
        "current_gate": GateType.VERDICT,
        "verdict": st.verdict or "pass",
        "status": "done" if (st.verdict or "pass") == "pass" else "changes-requested"
    }
