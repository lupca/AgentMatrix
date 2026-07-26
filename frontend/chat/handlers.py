import os
import logging
from typing import Any, AsyncGenerator, Dict, Optional

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
    Query Claude LLM with user question and task state context.
    Falls back to structured context response if API key is not configured.
    """
    context_str = build_system_context(state)
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        return (
            f"ℹ️ **Task Context**:\n{context_str}\n\n"
            f"**Question**: {message}\n\n"
            f"*(Note: ANTHROPIC_API_KEY not configured. Responding with active state context.)*"
        )

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        system_prompt = (
            "You are Control Tower Assistant. Answer user questions concisely based on the following task state context:\n\n"
            f"{context_str}"
        )
        response = await client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Error calling Claude API: {e}")
        return f"Error connecting to Claude: {e}\n\n**Current Context**:\n{context_str}"


async def chat_with_context_stream(
    message: str, state: Optional[Dict[str, Any]]
) -> AsyncGenerator[str, None]:
    """
    Stream responses from Claude with task state context.
    """
    context_str = build_system_context(state)
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not api_key:
        fallback_msg = (
            f"ℹ️ **Task Context**:\n{context_str}\n\n"
            f"**Question**: {message}\n\n"
            f"*(Note: ANTHROPIC_API_KEY not configured. Responding with active state context.)*"
        )
        for chunk in fallback_msg.split(" "):
            yield chunk + " "
        return

    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=api_key)
        system_prompt = (
            "You are Control Tower Assistant. Answer user questions concisely based on the following task state context:\n\n"
            f"{context_str}"
        )
        async with client.messages.stream(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1000,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text in stream.text_stream:
                yield text
    except Exception as e:
        logger.error(f"Error streaming from Claude API: {e}")
        err_msg = f"Error connecting to Claude: {e}\n\n**Current Context**:\n{context_str}"
        yield err_msg
