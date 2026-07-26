from typing import Any, Dict, Union
from app.graph.state import TaskState, GateType, Mode

def _get_state_obj(state: Union[TaskState, Dict[str, Any]]) -> TaskState:
    if isinstance(state, TaskState):
        return state
    return TaskState.model_validate(state)

def route_after_parse(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after parse_input node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    if not st.raw_input and not st.task_id:
        return "sync_to_db"
    return "spec_gate"

def route_after_spec(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after spec_gate node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    if st.awaiting_approval:
        return "approval"
    return "plan_gate"

def route_after_approval(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after approval node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    if st.awaiting_approval:
        # Pause execution, waiting for human approval resume
        return "approval"
    return "plan_gate"

def route_after_plan(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after plan_gate node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    if st.mode == Mode.PLAN_ONLY:
        return "sync_to_db"
    return "dispatch_gate"

def route_after_dispatch(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after dispatch_gate node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    return "review_order_gate"

def route_after_review_order(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after review_order_gate node."""
    st = _get_state_obj(state)
    if st.error:
        return "sync_to_db"
    return "verdict_gate"

def route_after_verdict(state: Union[TaskState, Dict[str, Any]]) -> str:
    """Route after verdict_gate node."""
    return "sync_to_db"
