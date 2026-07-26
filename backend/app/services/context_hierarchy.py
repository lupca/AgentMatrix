"""Hierarchical Context Service (Global/Project/Task)."""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.db.models import Project, Session as SessionModel, Task

logger = logging.getLogger(__name__)


class ContextHierarchy:
    """Compose 3-tiered prompt context (Global -> Project -> Task) with Anthropic prompt caching markers."""

    def __init__(self, db: DBSession):
        self.db = db
        self._global_context: list[dict[str, Any]] | None = None

    def _load_global(self) -> list[dict[str, Any]]:
        """Load global system prompt + gate rules."""
        global_file = os.path.join("app", "prompts", "global_context.md")
        if os.path.exists(global_file):
            try:
                with open(global_file, "r", encoding="utf-8") as f:
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

    def get_project_context(self, project_id: str | None) -> list[dict[str, Any]]:
        """Project description + context.md. Cached per session/request."""
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

        if not parts:
            return []

        return [
            {
                "role": "user",
                "content": "\n\n".join(parts),
            }
        ]

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
        """Compose 3 tiers (Global -> Project -> Task) with cache_control markers."""
        if not project_id and session.task_id:
            task = self.db.query(Task).filter(Task.id == session.task_id).first()
            if task and task.project:
                project_id = task.project

        messages: list[dict[str, Any]] = []

        # Tier 1: Global (with cache_control)
        global_ctx = self.get_global_context()
        if global_ctx:
            messages.extend(global_ctx)
            messages[-1]["cache_control"] = {"type": "ephemeral"}

        # Tier 2: Project (with cache_control)
        project_ctx = self.get_project_context(project_id)
        if project_ctx:
            messages.extend(project_ctx)
            messages[-1]["cache_control"] = {"type": "ephemeral"}

        # Tier 3: Task (dynamic - no cache_control)
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
        self.db.commit()
        return True
