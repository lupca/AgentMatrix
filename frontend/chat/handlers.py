import os
import sys
import logging
from typing import Any, AsyncGenerator, Dict, Optional

# Add backend to path for imports
backend_path = os.path.join(os.path.dirname(__file__), "..", "..", "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from app.services.llm import LLMClient

logger = logging.getLogger(__name__)


def format_result(result: Any) -> str:
    """
    Format LangGraph pipeline execution result into human-readable Markdown.
    """
    if not result:
        return "Pipeline executed with empty result."

    if hasattr(result, "model_dump"):
        result = result.model_dump()

    if isinstance(result, dict):
        task_id = result.get("task_id", "N/A")
        title = result.get("title", "")
        status = result.get("status", "todo")
        gate = result.get("current_gate", "spec")
        if hasattr(gate, "value"):
            gate = gate.value
        awaiting = result.get("awaiting_approval", False)
        prompt = result.get("approval_prompt")
        error = result.get("error")
        plan = result.get("plan")
        ac = result.get("acceptance_criteria", [])
        executor = result.get("executor")
        reviewer = result.get("reviewer")

        if error:
            return f"❌ **Error at Gate `{gate}`**: {error}"

        lines = []
        if gate == "spec" and awaiting:
            lines.append(f"Created {task_id}. Awaiting Spec Gate.")
        else:
            lines.append(f"📋 **Task**: `{task_id}`")

        if title:
            lines.append(f"**Title**: {title}")

        lines.append(f"**Current Gate**: `{gate}` | **Status**: `{status}`")

        if executor:
            lines.append(f"**Executor**: {executor}")
        if reviewer:
            lines.append(f"**Reviewer**: {reviewer}")

        if awaiting:
            lines.append(f"⚠️ **Awaiting Approval**: {prompt or 'Pending approval'}")

        if ac:
            ac_str = "\n".join(f"  - {item}" for item in ac)
            lines.append(f"**Acceptance Criteria**:\n{ac_str}")

        if plan:
            lines.append(f"**Implementation Plan**:\n```\n{plan}\n```")

        return "\n".join(lines)

    return str(result)


def build_system_context(state: Optional[Dict[str, Any]]) -> str:
    """Build context prompt string from task state."""
    if not state:
        return "No active task state found."

    if hasattr(state, "model_dump"):
        state = state.model_dump()

    context_parts = [
        f"Task ID: {state.get('task_id', 'None')}",
        f"Title: {state.get('title', 'None')}",
        f"Current Gate: {state.get('current_gate', 'None')}",
        f"Status: {state.get('status', 'None')}",
        f"Mode: {state.get('mode', 'None')}",
        f"Acceptance Criteria: {state.get('acceptance_criteria', [])}",
        f"Plan: {state.get('plan', 'None')}",
        f"Executor: {state.get('executor', 'None')}",
        f"Reviewer: {state.get('reviewer', 'None')}",
        f"Awaiting Approval: {state.get('awaiting_approval', False)}",
        f"Error: {state.get('error', 'None')}",
    ]
    return "\n".join(context_parts)


async def chat_with_context(message: str, state: Optional[Dict[str, Any]]) -> str:
    """
    Query LLM with user question and task state context.
    Falls back to structured context response if no API key is configured.
    """
    context_str = build_system_context(state)

    try:
        llm = LLMClient()
        system_prompt = (
            "You are Control Tower Assistant. Answer user questions concisely based on the following task state context:\n\n"
            f"{context_str}"
        )
        response = await llm.complete_async(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            max_tokens=1000
        )
        return response
    except Exception as e:
        logger.error(f"Error calling LLM API: {e}")
        return f"ℹ️ **Task Context**:\n{context_str}\n\n**Question**: {message}\n\n*(LLM error: {e})*"


async def chat_with_context_stream(
    message: str, state: Optional[Dict[str, Any]]
) -> AsyncGenerator[str, None]:
    """
    Stream responses from LLM with task state context.
    """
    context_str = build_system_context(state)

    try:
        llm = LLMClient()
        system_prompt = (
            "You are Control Tower Assistant. Answer user questions concisely based on the following task state context:\n\n"
            f"{context_str}"
        )
        async for chunk in llm.stream_async(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            max_tokens=1000
        ):
            yield chunk
    except Exception as e:
        logger.error(f"Error streaming from LLM API: {e}")
        err_msg = f"ℹ️ **Task Context**:\n{context_str}\n\n**Question**: {message}\n\n*(LLM error: {e})*"
        yield err_msg
