"""Hierarchical Context Service (Global/Project/Task)."""

from __future__ import annotations

import logging
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.db.models import Project, Session as SessionModel, Task
from app.core.config import settings
from app.graph.context import get_context_snapshot
from app.services import tool_definitions as _tool_definitions

logger = logging.getLogger(__name__)

# Project context (description + context.md + auto-memory) is capped so a
# large project doesn't dominate the cached prefix.
PROJECT_CONTEXT_MAX_CHARS = 25_000
_TRUNCATION_NOTICE = "\n\n[... project context truncated to fit 25KB cap ...]"

# Auto-memory: how many recently completed tasks to summarize per project.
PROJECT_MEMORY_TASK_LIMIT = 5


def drop_orphan_tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip assistant ``tool_calls`` entries and ``tool`` messages left unpaired.

    Safety net for any place a message list may be truncated (compaction,
    token budgeting) in a way that separates an assistant's tool_calls[]
    from the matching tool result — OpenAI-compatible APIs reject that
    pairing mismatch with a 400.
    """

    retained_call_ids = {
        str(call["id"])
        for message in messages
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id") is not None
    }
    retained_tool_ids = {
        str(message["tool_call_id"])
        for message in messages
        if message.get("role") == "tool" and message.get("tool_call_id") is not None
    }
    paired_call_ids = retained_call_ids & retained_tool_ids

    sanitized: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            message = dict(message)
            tool_calls = [
                call for call in message["tool_calls"]
                if isinstance(call, dict)
                and call.get("id") is not None
                and str(call["id"]) in paired_call_ids
            ]
            message.pop("tool_calls", None)
            if tool_calls:
                message["tool_calls"] = tool_calls
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if call_id is None or str(call_id) not in paired_call_ids:
                continue
        sanitized.append(message)
    return sanitized


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
        self.tool_result_replay_turns = max(0, settings.TOOL_RESULT_REPLAY_TURNS)

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
        """Baseline (eager) tool schemas sent alongside the system prompt.

        Rarely-used tools are loaded on demand via the ``load_tools``
        meta-tool inside the coordinator's tool loop instead of always
        occupying context (see ``app.services.tool_definitions``, ADR-001 §D3).
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

    def _task_header(self, session: SessionModel) -> dict[str, Any] | None:
        """Return the live task header as a post-snapshot suffix block."""
        if not session.task_id:
            return None
        task = self.db.query(Task).filter(Task.id == session.task_id).first()
        if not task:
            return None
        content = (
            f"You are Control Tower AI Assistant helping with Task [{task.id}]: "
            f"'{task.title}'. Project: '{task.project}', Status: '{task.status}'."
        )
        if task.plan:
            content += f"\nTask Plan:\n{task.plan}"
        graph_summary = self._graph_state_summary(session.task_id)
        if graph_summary:
            content += f"\n{graph_summary}"
        return {"role": "system", "content": content}

    @staticmethod
    def _tool_result_summary(message: dict[str, Any]) -> str:
        """Build a compact, decision-preserving representation of old output."""

        content = message.get("content", "")
        try:
            value = json.loads(content) if isinstance(content, str) else content
        except (TypeError, ValueError):
            value = content

        important = {
            "id", "task_id", "taskId", "verdict", "status", "action",
            "result", "error", "title", "constraints", "constraint",
        }

        def compact(item: Any) -> Any:
            if isinstance(item, dict):
                selected = {key: compact(val) for key, val in item.items() if key in important}
                return selected or {key: compact(val) for key, val in list(item.items())[:4]}
            if isinstance(item, list):
                return [compact(val) for val in item[:4]]
            return item

        detail = compact(value)
        rendered = json.dumps(detail, ensure_ascii=False, sort_keys=True) if not isinstance(detail, str) else detail
        if len(rendered) > 600:
            rendered = rendered[:597] + "..."
        tool_name = message.get("name") or message.get("tool_name") or "unknown"
        call_id = message.get("tool_call_id") or "unknown"
        return f"[Pruned tool result] tool={tool_name} tool_call_id={call_id} result={rendered}"

    def _replay_session_messages(self, session: SessionModel) -> list[dict[str, Any]]:
        """Replay history while retaining complete results for recent tool turns.

        Every tool *message* older than the replay window is summarized
        individually (not deduped per turn_id): a turn can carry several tool
        calls, and each one has its own tool_call_id that an earlier assistant
        message's tool_calls[] still points to. Dropping tool_call_id/name
        from a summary, or emitting only one summary for a multi-tool turn,
        leaves the provider unable to pair assistant tool_calls with their
        tool responses (OpenAI-compatible APIs reject that with a 400).
        """

        raw = [
            message for message in list(session.messages or [])
            if message.get("status", "complete") == "complete"
            and message.get("role") in {"user", "assistant", "tool", "system"}
        ]
        tool_turns: list[str] = []
        for message in raw:
            if message.get("role") == "tool" and message.get("turn_id"):
                turn_id = str(message["turn_id"])
                if turn_id not in tool_turns:
                    tool_turns.append(turn_id)
        # tool_turns[-0:] is the whole list, not "nothing" — with N=0 (keep
        # the 0 most recent turns full) a naive negative slice would keep
        # every turn instead of pruning all of them.
        full_turns = (
            set(tool_turns[-self.tool_result_replay_turns:])
            if self.tool_result_replay_turns > 0
            else set()
        )

        replay: list[dict[str, Any]] = []
        for message in raw:
            if message.get("role") != "tool" or not message.get("turn_id"):
                replay.append(message)
                continue
            turn_id = str(message["turn_id"])
            if turn_id in full_turns:
                replay.append(message)
                continue
            summary: dict[str, Any] = {
                "role": "tool",
                "turn_id": turn_id,
                "content": self._tool_result_summary(message),
            }
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                summary["tool_call_id"] = tool_call_id
            name = message.get("name") or message.get("tool_name")
            if name:
                summary["name"] = name
            replay.append(summary)
        return replay

    def get_task_context(
        self,
        session: SessionModel,
        *,
        include_latest_user: bool = True,
        include_task_header: bool = True,
    ) -> list[dict[str, Any]]:
        """Task header plus durable session history, with bounded tool replay."""
        messages: list[dict[str, Any]] = []

        if include_task_header:
            task_header = self._task_header(session)
            if task_header:
                messages.append(task_header)

        history = self._replay_session_messages(session)
        latest_user_index = next(
            (index for index in range(len(history) - 1, -1, -1) if history[index].get("role") == "user"),
            None,
        )
        for index, message in enumerate(history):
            if not include_latest_user and index == latest_user_index:
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
        current_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Compose tiers in increasing order of volatility:

        Global -> Project -> append-only task/history -> Snapshot -> latest user.
        The snapshot is deliberately last in the stable prefix so mutations do
        not invalidate the history that precedes it.
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

        # Task/history precedes the dynamic snapshot. The newest user message
        # is placed after the snapshot as the request-specific suffix.
        task_context = self.get_task_context(
            session,
            include_latest_user=current_turn_id is None,
            include_task_header=False,
        )
        messages.extend(task_context)

        # Dynamic snapshot: keep this as the last prefix block.
        messages.append(
            {"role": "system", "content": get_context_snapshot(session, self.db)}
        )

        task_header = self._task_header(session)
        if task_header:
            messages.append(task_header)

        if current_turn_id and session.messages:
            latest_user = next(
                (message for message in reversed(session.messages)
                 if message.get("role") == "user"
                 and message.get("turn_id") == current_turn_id
                 and message.get("status", "complete") == "complete"),
                None,
            )
            if latest_user:
                messages.append({
                    key: latest_user[key] for key in ("role", "content", "name", "tool_name", "tool_call_id", "tool_calls")
                    if key in latest_user
                })

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

        # Do not cut through an assistant tool call/result pair.  A plain
        # suffix slice can retain a tool result while dropping the assistant
        # message that declared its tool_call_id (or vice versa), producing
        # an invalid OpenAI-compatible request on the next replay.
        start = max(0, len(raw_msgs) - 10)
        while True:
            assistant_by_call_id: dict[str, int] = {}
            tool_by_call_id: dict[str, int] = {}
            for index, message in enumerate(raw_msgs):
                if message.get("role") == "assistant":
                    for call in message.get("tool_calls") or []:
                        if isinstance(call, dict) and call.get("id") is not None:
                            assistant_by_call_id[str(call["id"])] = index
                elif message.get("role") == "tool" and message.get("tool_call_id") is not None:
                    tool_by_call_id[str(message["tool_call_id"])] = index

            expanded_start = start
            for call_id, assistant_index in assistant_by_call_id.items():
                tool_index = tool_by_call_id.get(call_id)
                if tool_index is None:
                    continue
                if assistant_index >= start or tool_index >= start:
                    expanded_start = min(expanded_start, assistant_index, tool_index)
            if expanded_start == start:
                break
            start = expanded_start

        # ``start`` may have been pushed earlier than the original -10 cut to
        # avoid splitting a tool call/result pair, so the count below must
        # reflect the boundary actually used, not the pre-expansion guess.
        if summary is None:
            summary = (
                f"[Context Compaction: Summarized {start} previous messages "
                "in conversation history]"
            )

        kept = [dict(message) for message in raw_msgs[start:]]
        kept = drop_orphan_tool_pairs(kept)
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
