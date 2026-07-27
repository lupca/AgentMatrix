"""Hierarchical Context Service (Global/Project/Task)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.db.models import Project, Session as SessionModel, Task
from app.graph.context import get_context_snapshot
from app.services import tool_definitions as _tool_definitions

logger = logging.getLogger(__name__)

# Project context (description + context.md + auto-memory) is capped so a
# large project doesn't dominate the cached prefix.
PROJECT_CONTEXT_MAX_CHARS = 25_000
_TRUNCATION_NOTICE = "\n\n[... project context truncated to fit 25KB cap ...]"

# Auto-memory: how many recently completed tasks to summarize per project.
PROJECT_MEMORY_TASK_LIMIT = 5


class ContextHierarchy:
    """Compose 3-tiered prompt context (Global -> Project -> Task) with Anthropic prompt caching markers.

    Optionally integrates with a compiled LangGraph pipeline (see
    ``app.graph.builder.build_graph``): when ``graph`` is provided, the Task
    tier enriches its system message with the gate pipeline's live state
    (current gate, verdict, findings) read via the graph's checkpointer.
    """

    def __init__(self, db: DBSession, graph: Any | None = None):
        self.db = db
        self._global_context: list[dict[str, Any]] | None = None
        self.graph = graph

    def _load_global(self) -> list[dict[str, Any]]:
        """Load global system prompt + gate rules."""
        global_file = Path(__file__).resolve().parents[1] / "prompts" / "global_context.md"
        if os.path.exists(global_file):
            try:
                with global_file.open("r", encoding="utf-8") as f:
                    content = f.read()
                return [{"role": "system", "content": content}]
            except Exception as e:
                logger.warning("Failed to read global_context.md: %s", e)

        default_prompt = (
            "You are Control Tower V2, an intelligent task coordination and execution assistant.\n"
            "Follow strict gate validation rules:\n"
            "- Spec Gate: Generate clear title and acceptance criteria (AC).\n"
            "- Plan Gate: Create explicit step-by-step execution plans.\n"
            "- Dispatch Gate: Assign executors to tasks.\n"
            "- Review-Order Gate: Request independent review sheets.\n"
            "- Verdict Gate: Enforce four-eyes rule (reviewer != executor)."
        )
        return [{"role": "system", "content": default_prompt}]

    def get_global_context(self) -> list[dict[str, Any]]:
        """Global System prompt + gate rules. Cached in memory per service instance."""
        if self._global_context is None:
            self._global_context = self._load_global()
        return [dict(m) for m in self._global_context]

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Tool schemas that belong to the global context, alongside the system prompt.

        Rarely-used tools are marked ``defer_loading`` (see
        ``app.services.tool_definitions``) so their schemas are appended on
        demand instead of always occupying context.
        """
        return _tool_definitions.get_tool_definitions()

    def _project_memory(self, project_id: str) -> str | None:
        """Auto-memory: summarize recently completed tasks for this project.

        Lets the project tier "learn" from prior sessions without an LLM
        call, using structured outcomes already recorded on ``Task``.
        """
        tasks = (
            self.db.query(Task)
            .filter(Task.project == project_id, Task.status == "done")
            .order_by(Task.updated_at.desc())
            .limit(PROJECT_MEMORY_TASK_LIMIT)
            .all()
        )
        if not tasks:
            return None
        lines = [f"- [{t.id}] {t.title} (verdict: {t.verdict or 'n/a'})" for t in tasks]
        return "[Project Memory: recent completed tasks]\n" + "\n".join(lines)

    def get_project_context(self, project_id: str | None) -> list[dict[str, Any]]:
        """Project description + context.md + auto-memory, capped at 25KB. Cached per session/request."""
        if not project_id:
            return []
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return []

        parts: list[str] = []
        if project.description:
            parts.append(f"[Project Context: {project.name}]\n{project.description}")

        if getattr(project, "context_md", None):
            parts.append(f"[Project Context Details]\n{project.context_md}")
        else:
            context_file = os.path.join("projects", project_id, "context.md")
            if os.path.exists(context_file):
                try:
                    with open(context_file, "r", encoding="utf-8") as f:
                        parts.append(f"[Project Context Details]\n{f.read()}")
                except Exception as e:
                    logger.warning("Failed to read context file for %s: %s", project_id, e)

        memory = self._project_memory(project_id)
        if memory:
            parts.append(memory)

        if not parts:
            return []

        content = "\n\n".join(parts)
        if len(content) > PROJECT_CONTEXT_MAX_CHARS:
            keep = PROJECT_CONTEXT_MAX_CHARS - len(_TRUNCATION_NOTICE)
            content = content[: max(0, keep)] + _TRUNCATION_NOTICE

        return [
            {
                "role": "user",
                "content": content,
            }
        ]

    def _graph_state_summary(self, task_id: str | None) -> str | None:
        """Read live gate-pipeline state for ``task_id`` from the LangGraph checkpointer."""
        if not self.graph or not task_id:
            return None
        try:
            snapshot = self.graph.get_state({"configurable": {"thread_id": task_id}})
        except Exception as e:
            logger.warning("Failed to read LangGraph checkpoint for task %s: %s", task_id, e)
            return None

        values = getattr(snapshot, "values", None) if snapshot else None
        if not values:
            return None

        parts: list[str] = []
        gate = values.get("current_gate")
        if gate is not None:
            parts.append(f"current_gate={getattr(gate, 'value', gate)}")
        verdict = values.get("verdict")
        if verdict:
            parts.append(f"verdict={verdict}")
        findings = values.get("findings")
        if findings:
            parts.append(f"findings={'; '.join(findings)}")
        if not parts:
            return None
        return "[LangGraph State] " + ", ".join(parts)

    def get_task_context(self, session: SessionModel) -> list[dict[str, Any]]:
        """Session messages for current task."""
        messages: list[dict[str, Any]] = []

        if session.task_id:
            task = self.db.query(Task).filter(Task.id == session.task_id).first()
            if task:
                content = (
                    f"You are Control Tower AI Assistant helping with Task [{task.id}]: "
                    f"'{task.title}'. Project: '{task.project}', Status: '{task.status}'."
                )
                if task.plan:
                    content += f"\nTask Plan:\n{task.plan}"
                graph_summary = self._graph_state_summary(session.task_id)
                if graph_summary:
                    content += f"\n{graph_summary}"
                messages.append({"role": "system", "content": content})

        for message in list(session.messages or []):
            if message.get("status", "complete") != "complete":
                continue
            if message.get("role") not in {"user", "assistant", "tool", "system"}:
                continue
            item = {
                "role": message["role"],
                "content": str(message.get("content", "")),
            }
            for k in ("name", "tool_name", "tool_call_id", "tool_calls"):
                if k in message:
                    item[k] = message[k]
            messages.append(item)

        return messages

    def build_messages(
        self,
        session: SessionModel,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compose tiers in increasing order of volatility:

        Global (static) -> Project (semi-stable) -> Snapshot (dynamic) ->
        Task/session (dynamic). Global and Project messages are marked
        ``pinned: True`` so ``budget_messages`` keeps them as a stable
        prefix; the snapshot is its own message so a project/task mutation
        no longer rewrites the Global tier's bytes.
        """
        if not project_id and session.task_id:
            task = self.db.query(Task).filter(Task.id == session.task_id).first()
            if task and task.project:
                project_id = task.project

        messages: list[dict[str, Any]] = []

        # Tier 1: Global (static, pinned)
        global_ctx = self.get_global_context()
        if global_ctx:
            messages.extend(global_ctx)
            messages[-1]["pinned"] = True

        # Tier 2: Project (semi-stable, pinned)
        project_ctx = self.get_project_context(project_id)
        if project_ctx:
            messages.extend(project_ctx)
            messages[-1]["pinned"] = True

        # Tier 2.5: Context snapshot (dynamic, own message - not pinned)
        messages.append(
            {"role": "system", "content": get_context_snapshot(session, self.db)}
        )

        # Tier 3: Task (dynamic - not pinned)
        messages.extend(self.get_task_context(session))

        return messages

    def compact_context(
        self,
        session: SessionModel,
        threshold: int = 50,
        summary: str | None = None,
    ) -> bool:
        """Compact session messages when message count exceeds threshold."""
        raw_msgs = list(session.messages or [])
        if len(raw_msgs) <= threshold:
            return False

        older_count = max(0, len(raw_msgs) - 10)
        if summary is None:
            summary = (
                f"[Context Compaction: Summarized {older_count} previous messages "
                "in conversation history]"
            )

        kept = raw_msgs[-10:] if len(raw_msgs) > 10 else raw_msgs
        summary_msg = {
            "id": f"msg-compact-{session.id}",
            "role": "system",
            "content": summary,
            "status": "complete",
        }
        session.messages = [summary_msg] + kept
        session.message_count = len(session.messages)
        self.db.commit()
        return True
