import os
import logging
from typing import Any, Optional, Union
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from app.graph.state import TaskState
from app.graph.nodes import (
    parse_input,
    spec_gate,
    approval,
    plan_gate,
    dispatch_gate,
    review_order_gate,
    verdict_gate,
    sync_to_db,
    log_action,
)
from app.graph.router import (
    route_after_parse,
    route_after_spec,
    route_after_approval,
    route_after_plan,
    route_after_dispatch,
    route_after_review_order,
    route_after_verdict,
)

logger = logging.getLogger(__name__)

def get_postgres_saver(conn_string: Optional[str] = None):
    """Instantiate PostgresSaver checkpointer if available."""
    db_uri = conn_string or os.getenv("DATABASE_URL")
    if not db_uri:
        logger.info("No Postgres DATABASE_URL provided, falling back to MemorySaver")
        return MemorySaver()
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
        # Return PostgresSaver from conn string or pool
        conn = psycopg.connect(db_uri)
        saver = PostgresSaver(conn)
        saver.setup()
        return saver
    except Exception as e:
        logger.warning(f"Failed to initialize PostgresSaver ({e}), falling back to MemorySaver")
        return MemorySaver()

def build_graph(
    checkpointer: Any = None,
    postgres_conn: Optional[str] = None,
    interrupt_before: Optional[list] = None
):
    """Build and compile the LangGraph StateGraph pipeline."""
    builder = StateGraph(TaskState)

    # 1. Add nodes
    builder.add_node("parse_input", parse_input)
    builder.add_node("spec_gate", spec_gate)
    builder.add_node("approval", approval)
    builder.add_node("plan_gate", plan_gate)
    builder.add_node("dispatch_gate", dispatch_gate)
    builder.add_node("review_order_gate", review_order_gate)
    builder.add_node("verdict_gate", verdict_gate)
    builder.add_node("sync_to_db", sync_to_db)
    builder.add_node("log_action", log_action)

    # 2. Add edges & conditional transitions
    builder.add_edge(START, "parse_input")

    builder.add_conditional_edges(
        "parse_input",
        route_after_parse,
        {
            "spec_gate": "spec_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "spec_gate",
        route_after_spec,
        {
            "approval": "approval",
            "plan_gate": "plan_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "approval": "approval",
            "plan_gate": "plan_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "plan_gate",
        route_after_plan,
        {
            "dispatch_gate": "dispatch_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "dispatch_gate",
        route_after_dispatch,
        {
            "review_order_gate": "review_order_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "review_order_gate",
        route_after_review_order,
        {
            "verdict_gate": "verdict_gate",
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_conditional_edges(
        "verdict_gate",
        route_after_verdict,
        {
            "sync_to_db": "sync_to_db",
        },
    )

    builder.add_edge("sync_to_db", "log_action")
    builder.add_edge("log_action", END)

    # 3. Determine checkpointer
    if checkpointer is None:
        if postgres_conn or os.getenv("DATABASE_URL"):
            checkpointer = get_postgres_saver(postgres_conn)
        else:
            checkpointer = MemorySaver()

    return builder.compile(checkpointer=checkpointer, interrupt_before=interrupt_before)
