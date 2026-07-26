import sys
from pathlib import Path
from uuid import uuid4
import chainlit as cl

# Ensure backend directory is in sys.path for app.graph imports
root_dir = Path(__file__).resolve().parent.parent.parent
backend_dir = root_dir / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.graph.builder import build_graph
from router import route_message
from handlers import format_result, chat_with_context_stream


@cl.on_chat_start
async def start():
    """Initialize LangGraph instance and thread session for Chainlit chat."""
    graph = build_graph()
    thread_id = cl.user_session.get("thread_id") or str(uuid4())

    cl.user_session.set("graph", graph)
    cl.user_session.set("thread_id", thread_id)

    await cl.Message(
        content=f"🚀 **Control Tower Chat UI** initialized.\nSession Thread ID: `{thread_id}`"
    ).send()


@cl.on_message
async def main(message: cl.Message):
    """Handle incoming messages, route to pipeline or streaming LLM chat."""
    graph = cl.user_session.get("graph")
    thread_id = cl.user_session.get("thread_id")

    if not graph:
        graph = build_graph()
        cl.user_session.set("graph", graph)
    if not thread_id:
        thread_id = str(uuid4())
        cl.user_session.set("thread_id", thread_id)

    config = {"configurable": {"thread_id": thread_id}}
    route = route_message(message.content)

    if route == "pipeline":
        # 0 tokens - direct pipeline execution
        result = await graph.ainvoke({"raw_input": message.content}, config)
        formatted_output = format_result(result)
        await cl.Message(content=formatted_output).send()
    else:
        # Chat with state context - streaming LLM response
        state = graph.get_state(config)
        state_values = state.values if state else {}

        msg = cl.Message(content="")
        async for chunk in chat_with_context_stream(message.content, state_values):
            await msg.stream_token(chunk)
        await msg.send()
