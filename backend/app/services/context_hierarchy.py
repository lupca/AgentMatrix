"""Hierarchical Context Service (Global/Project/Task)."""

from __future__ import annotations

import logging
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.db.models import GateRecord, Project, Session as SessionModel, Task, TaskEvent
from app.core.config import settings
from app.core.compression import count_tokens, context_window_for_model
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

    The Task tier enriches its system message with the authoritative task
    lifecycle state persisted by TaskOrchestrationService and GateRecord.
    """

    def __init__(self, db: DBSession):
        self.db = db
        self._global_context: list[dict[str, Any]] | None = None
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
        """Read authoritative gate state persisted for ``task_id``."""
        if not task_id:
            return None
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if task is None:
            return None

        parts: list[str] = []
        if task.current_gate:
            parts.append(f"current_gate={task.current_gate}")
        verdict = task.verdict
        if verdict:
            parts.append(f"verdict={verdict}")
        findings = task.findings or []
        if not findings:
            latest_verdict = (
                self.db.query(GateRecord)
                .filter(
                    GateRecord.task_id == task_id,
                    GateRecord.gate_type == "verdict",
                )
                .order_by(GateRecord.id.desc())
                .first()
            )
            payload = (latest_verdict.input_payload or {}) if latest_verdict else {}
            findings = payload.get("findings", [])
        if findings:
            parts.append(f"findings={'; '.join(map(str, findings))}")
        if not parts:
            return None
        return "[Task Gate State] " + ", ".join(parts)

    def _get_recent_task_events(
        self,
        task_id: str,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve recent task events from task_events table for LLM context (CTV2-117)."""
        if not task_id:
            return []
        query = self.db.query(TaskEvent).filter(TaskEvent.task_id == task_id)
        if since is not None:
            query = query.filter(TaskEvent.created_at > since)
        events = query.order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc()).all()

        return [
            {
                "type": e.event_type,
                "payload": e.payload,
                "at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

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
        gate_summary = self._graph_state_summary(session.task_id)
        if gate_summary:
            content += f"\n{gate_summary}"
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
            and message.get("kind") != "task_rollup"
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
        since: datetime | None = None,
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

        # Inject recent task events if task_id exists
        if session.task_id:
            recent_events = self._get_recent_task_events(session.task_id, since=since)
            if recent_events:
                messages.append({
                    "role": "system",
                    "content": f"Recent task events:\n{json.dumps(recent_events, ensure_ascii=False)}",
                })

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
        threshold: int | None = None,
        summary: str | None = None,
        *,
        model: str | None = None,
        context_window: int | None = None,
        threshold_ratio: float | None = None,
        summarizer: Any | None = None,
        agent: Any | None = None,
    ) -> bool:
        """Summarize old history when it reaches the active model's budget.

        ``threshold`` is retained as an explicit token threshold for callers
        that need to force a deterministic boundary (``0`` forces it). The
        normal path uses a ratio of the active model's context window. A
        failed summarizer deliberately leaves the session exactly as it was.
        """
        raw_msgs = list(session.messages or [])
        if any(message.get("id") == f"msg-compact-{session.id}" for message in raw_msgs):
            # A compaction summary is part of the stable prefix. Do not
            # regenerate it on every turn and invalidate the provider cache.
            return False

        if threshold is None:
            window = context_window or context_window_for_model(model)
            ratio = threshold_ratio if threshold_ratio is not None else settings.COMPACTION_THRESHOLD_RATIO
            threshold_tokens = max(1, int(window * ratio))
        else:
            threshold_tokens = max(0, threshold)
        if threshold_tokens > 0 and count_tokens(raw_msgs) <= threshold_tokens:
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
            old_messages = raw_msgs[:start]
            if not old_messages:
                return False
            if summarizer is None:
                from app.services.llm_service import LLMService

                service = LLMService()

                def summarizer(messages: list[dict[str, Any]], **kwargs: Any) -> str:
                    response = service.complete_sync(agent, messages, **kwargs)
                    return response.text
            prompt = (
                "Summarize this conversation for future turns. Preserve exactly "
                "all confirmed decisions, task IDs, result_ref values, verdicts, "
                "acceptance criteria, constraints, and unresolved questions. "
                "Do not invent facts. Return only the useful summary, not a "
                "placeholder or commentary about summarization.\n\n"
                + json.dumps(old_messages, ensure_ascii=False, default=str)
            )
            try:
                summary = summarizer(
                    [{"role": "user", "content": prompt}],
                    model=getattr(settings, "COMPACTION_MODEL", None) or model,
                    max_tokens=settings.COMPACTION_MAX_OUTPUT_TOKENS,
                    temperature=0,
                    operation="context_compaction",
                    session_id=session.id,
                    task_id=session.task_id,
                    db_session=self.db,
                )
            except Exception:
                logger.warning("Context compaction summarization failed for session %s", session.id, exc_info=True)
                return False
            if hasattr(summary, "text"):
                summary = summary.text
            if not isinstance(summary, str) or not summary.strip() or summary.lstrip().startswith("[Context Compaction:"):
                logger.warning("Context compaction returned no usable summary for session %s", session.id)
                return False

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
