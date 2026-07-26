import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.graph.state import TaskState, GateType, Mode
from app.graph.nodes import (
    parse_input,
    spec_gate,
    plan_gate,
    dispatch_gate,
    review_order_gate,
    verdict_gate,
    sync_to_db,
    log_action,
)
from app.graph.builder import build_graph

def test_parse_input_with_pm_prefix():
    state = TaskState(raw_input="/pm CTV2-999 Test Title")
    res = parse_input(state)
    assert res["task_id"] == "CTV2-999"
    assert res["title"] == "Test Title"
    assert res["current_gate"] == GateType.SPEC

def test_parse_input_raw_text():
    state = TaskState(raw_input="Create simple feature")
    res = parse_input(state)
    assert res["task_id"].startswith("TASK-")
    assert res["title"] == "Create simple feature"

def test_verdict_gate_four_eyes_violation():
    state = TaskState(
        executor="@same_person",
        reviewer="@same_person"
    )
    res = verdict_gate(state)
    assert res["verdict"] == "changes"
    assert res["status"] == "changes-requested"
    assert "Four-eyes rule violation" in res["error"]

def test_verdict_gate_pass():
    state = TaskState(
        executor="@dev",
        reviewer="@reviewer"
    )
    res = verdict_gate(state)
    assert res["verdict"] == "pass"
    assert res["status"] == "done"

def test_graph_compile_and_invoke():
    memory = MemorySaver()
    graph = build_graph(checkpointer=memory)
    config = {"configurable": {"thread_id": "test-thread-1"}}

    # Invoke graph with initial input
    result = graph.invoke({"raw_input": "/pm CTV2-003 LangGraph Core", "mode": Mode.BYPASS}, config)
    assert result["task_id"] == "CTV2-003"
    assert result["title"] == "LangGraph Core"
    assert result["current_gate"] == GateType.VERDICT
    assert result["status"] == "done"

def test_graph_thread_resume():
    memory = MemorySaver()
    graph = build_graph(checkpointer=memory)
    config = {"configurable": {"thread_id": "test-thread-resume"}}

    # Initial invoke
    res1 = graph.invoke({"raw_input": "/pm TASK-100 Resume Task", "mode": Mode.SUPERVISED}, config)
    assert res1["task_id"] == "TASK-100"

    # Resume invoke on existing thread
    res2 = graph.invoke(None, config)
    assert res2["task_id"] == "TASK-100"
