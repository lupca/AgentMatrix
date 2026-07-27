"""CLI-backed coordinator with durable PostgreSQL session history."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.db.models import Agent as AgentModel, LLMUsage, Session as SessionModel, Task as TaskModel
from app.services.llm_client import (
    ANTHROPIC_MODEL,
    GOOGLE_MODEL,
    OPENAI_MODEL,
    UsageCounts,
    calculate_cost,
)
from app.services.cli_dispatcher import (
    CLIDispatchError,
    CLIDispatcher,
    format_history_as_prompt,
    route_model,
)
from app.services.context_hierarchy import ContextHierarchy, drop_orphan_tool_pairs
from app.services.crypto import decrypt_api_key
from app.services.command_router import CommandRouter
from app.services.tool_registry import get_group_tool_definitions, get_spec, resolve_tool_name
from app.graph.context import invalidate_context_snapshot
from app.services.providers import CoordinatorProvider, ProviderResponse
from app.services.providers.openai_adapter import OpenAIAdapter


logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOWS = {
    "claude": 200_000,
    "gemini": 1_000_000,
    "gpt": 128_000,
}


@dataclass(frozen=True)
class CoordinatorResult:
    """A completed, durably persisted coordinator turn."""

    content: str
    message_id: str
    turn_id: str
    provider: str
    model: str
    cached: bool = False


class ProviderRouter:
    """Resolve the OpenAI-compatible coordinator adapter from an Agent DB record.

    Anthropic and Google models have no SDK-direct adapter; they always
    dispatch through the CLI (see ``route_model``).
    """

    def __init__(
        self,
        providers: Mapping[str, CoordinatorProvider] | None = None,
    ):
        self.providers: dict[str, CoordinatorProvider] = dict(providers or {})

    @staticmethod
    def provider_name(model: str) -> str:
        normalized = (model or "").lower()
        if "claude" in normalized:
            return "anthropic"
        if "gemini" in normalized:
            return "google"
        if normalized.startswith(("gpt-", "o1-", "chatgpt-")):
            return "openai"
        raise ValueError(
            f"Cannot infer provider for coordinator model '{model}'. "
            "Specify provider='anthropic', provider='google', or provider='openai'."
        )

    def get(
        self,
        model: str,
        provider: str | None = None,
        *,
        agent: AgentModel | None = None,
    ) -> CoordinatorProvider:
        provider_name = (
            provider
            or (agent.provider if agent is not None else self.provider_name(model))
        ).lower()
        if provider_name == "openai" and agent is not None:
            if not agent.api_key:
                raise ValueError(
                    f"Coordinator agent '{agent.id}' does not have an API key"
                )
            return OpenAIAdapter(
                api_key=decrypt_api_key(agent.api_key),
                base_url=agent.base_url,
            )
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            if provider_name == "openai":
                raise ValueError(
                    "No OpenAI coordinator agent is configured for this model"
                ) from exc
            raise ValueError(
                f"Unsupported coordinator provider '{provider_name}'. "
                "Only 'openai' resolves to a coordinator adapter; anthropic/google "
                "models dispatch through the CLI."
            ) from exc

    # Friendly alias for call sites and tests.
    route = get


DEFAULT_PROVIDER_ROUTER = ProviderRouter()


class CoordinatorService:
    """Own coordinator sessions, CLI dispatch, retries, and telemetry.

    Coordinator turns run on exactly two paths: the OpenAI-compatible API
    (via ``ProviderRouter``/``OpenAIAdapter``) or an account-backed CLI
    (``route_model`` + ``CLIDispatcher``). The ``router``/``providers``
    constructor arguments let tests inject a fake OpenAI adapter without
    a real Agent DB record.
    """

    _session_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        db: DBSession,
        *,
        dispatcher: CLIDispatcher | None = None,
        cli_dispatcher: CLIDispatcher | None = None,
        router: ProviderRouter | None = None,
        providers: Mapping[str, CoordinatorProvider] | None = None,
        max_retries: int = 2,
        retry_base_seconds: float = 0.25,
        max_output_tokens: int = 2048,
        context_windows: Mapping[str, int] | None = None,
        context_safety_tokens: int = 1024,
        max_tool_iterations: int | None = None,
        max_turn_tokens: int | None = None,
        max_repeated_tool_calls: int | None = None,
        graph: Any | None = None,
    ):
        if dispatcher is not None and cli_dispatcher is not None:
            raise ValueError("Pass either dispatcher or cli_dispatcher, not both")
        if router is not None and providers is not None:
            raise ValueError("Pass either router or providers, not both")
        self.db = db
        self.dispatcher = dispatcher or cli_dispatcher or CLIDispatcher()
        # Optional compiled LangGraph pipeline (see app.graph.builder.build_graph).
        # When provided, ContextHierarchy enriches Task-tier context with live
        # gate state read from the graph's checkpointer.
        self.graph = graph
        if router is not None:
            self.router = router
        elif providers is not None:
            self.router = ProviderRouter(providers)
        else:
            self.router = DEFAULT_PROVIDER_ROUTER
        self.max_retries = max(0, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)
        self.max_output_tokens = max(1, max_output_tokens)
        self.context_windows = dict(context_windows or DEFAULT_CONTEXT_WINDOWS)
        self.context_safety_tokens = max(0, context_safety_tokens)

        if max_tool_iterations is None:
            max_tool_iterations = settings.COORDINATOR_MAX_TOOL_ITERATIONS
        self.max_tool_iterations = max(1, max_tool_iterations)

        if max_turn_tokens is None:
            max_turn_tokens = settings.COORDINATOR_MAX_TURN_TOKENS
        self.max_turn_tokens = max(1, max_turn_tokens)

        if max_repeated_tool_calls is None:
            max_repeated_tool_calls = settings.COORDINATOR_MAX_REPEATED_TOOL_CALLS
        self.max_repeated_tool_calls = max(1, max_repeated_tool_calls)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_or_create_session(self, thread_id: str) -> SessionModel:
        """Resolve a session/task identifier or create a standalone session."""

        db_session = (
            self.db.query(SessionModel)
            .filter(
                (SessionModel.id == thread_id)
                | (SessionModel.thread_id == thread_id)
            )
            .first()
        )
        if db_session:
            return db_session

        task = (
            self.db.query(TaskModel)
            .filter((TaskModel.id == thread_id) | (TaskModel.session_id == thread_id))
            .first()
        )
        if task:
            if not task.session_id:
                task.session_id = str(uuid.uuid4())
                self.db.commit()
                self.db.refresh(task)
            db_session = (
                self.db.query(SessionModel)
                .filter(SessionModel.task_id == task.id)
                .first()
            )
            if db_session is None:
                db_session = SessionModel(
                    id=task.session_id,
                    task_id=task.id,
                    project_id=task.project,
                    context_level="task",
                    thread_id=task.session_id,
                    messages=[],
                )
        else:
            db_session = SessionModel(
                id=thread_id,
                thread_id=thread_id,
                messages=[],
            )

        self.db.add(db_session)
        self.db.commit()
        self.db.refresh(db_session)
        return db_session

    def append_message(
        self,
        db_session: SessionModel,
        *,
        role: str,
        content: str,
        message_id: str | None = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        """Append one canonical message to PostgreSQL-backed session history."""

        message = {
            "id": message_id or f"msg-{uuid.uuid4()}",
            "role": role,
            "content": content,
            "timestamp": self._now(),
            **metadata,
        }
        db_session.messages = list(db_session.messages or []) + [message]
        db_session.message_count = len(db_session.messages)
        db_session.last_activity_at = datetime.now(timezone.utc)
        self.db.commit()
        return message

    def _resolve_selection(
        self,
        db_session: SessionModel,
        model: str | None,
        provider: str | None,
    ) -> tuple[str, str, CoordinatorProvider | None]:
        requested_model = model
        if requested_model is None and provider is None:
            requested_model = db_session.selected_model
            provider = db_session.selected_provider
        elif requested_model is None and provider == db_session.selected_provider:
            requested_model = db_session.selected_model

        provider = provider.lower() if provider else None
        if requested_model is None:
            if provider == "google":
                requested_model = GOOGLE_MODEL
            elif provider == "anthropic":
                requested_model = ANTHROPIC_MODEL
            elif provider == "openai":
                requested_model = OPENAI_MODEL
            else:
                requested_model = os.getenv("COORDINATOR_MODEL", ANTHROPIC_MODEL)

        if provider != "openai":
            # Explicit OpenAI selections are resolved from the DB below;
            # other providers still use the model-to-CLI router.
            route = route_model(requested_model, provider)
            provider = provider or route.provider
        if provider == "codex":
            provider = "openai"

        # Only the OpenAI-compatible API path resolves an adapter; anthropic
        # and google models always dispatch through a CLI account login.
        adapter: CoordinatorProvider | None = None
        if provider == "openai":
            agent = (
                self.db.query(AgentModel)
                .filter(
                    AgentModel.model == requested_model,
                    AgentModel.provider == "openai",
                    AgentModel.agent_type == "api",
                )
                .order_by(AgentModel.is_default.desc(), AgentModel.id)
                .first()
            )
            adapter = self.router.get(requested_model, provider, agent=agent)
        return provider, requested_model, adapter

    def validate_selection(
        self,
        db_session: SessionModel,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> tuple[str, str]:
        """Validate a requested selection without making a provider call."""

        provider_name, resolved_model, _ = self._resolve_selection(
            db_session,
            model,
            provider,
        )
        return provider_name, resolved_model

    def _task_system_message(self, db_session: SessionModel) -> dict[str, str] | None:
        if not db_session.task_id:
            return None
        task = (
            self.db.query(TaskModel)
            .filter(TaskModel.id == db_session.task_id)
            .first()
        )
        if task is None:
            return None
        content = (
            f"You are Control Tower AI Assistant helping with Task [{task.id}]: "
            f"'{task.title}'. Project: '{task.project}', Status: '{task.status}'."
        )
        if task.plan:
            content += f"\nTask Plan:\n{task.plan}"
        return {"role": "system", "content": content}

    @staticmethod
    def estimate_tokens(message: dict[str, Any]) -> int:
        """Conservative dependency-free token estimate for context budgeting."""

        content = str(message.get("content", ""))
        return max(1, (len(content) + 3) // 4) + 4

    def _context_window(self, model: str) -> int:
        normalized = model.lower()
        for model_key, token_limit in self.context_windows.items():
            if model_key.lower() in normalized:
                return token_limit
        return min(self.context_windows.values(), default=128_000)

    def budget_messages(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        max_output_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        """Keep the stable system/header prefix and newest turns within model context."""

        output_budget = max_output_tokens or self.max_output_tokens
        available = max(
            1,
            self._context_window(model)
            - output_budget
            - self.context_safety_tokens,
        )
        # ContextHierarchy places the snapshot after append-only history. Keep
        # that ordering intact: collecting every system message separately
        # would move the history behind the snapshot and defeat prefix reuse.
        snapshot_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("role") == "system"
                and str(message.get("content", "")).startswith("## System State")
            ),
            None,
        )
        if snapshot_index is None:
            prefix = [
                message for message in messages
                if message.get("role") == "system" or message.get("pinned")
            ]
            conversation = [
                message for message in messages
                if message not in prefix
                and message.get("status", "complete") == "complete"
            ]
        else:
            prefix = messages[: snapshot_index + 1]
            conversation = [
                message for message in messages[snapshot_index + 1 :]
                if message.get("status", "complete") == "complete"
            ]

        selected_prefix: list[dict[str, Any]] = []
        used = 0
        for message in prefix:
            tokens = self.estimate_tokens(message)
            if used + tokens <= available:
                selected_prefix.append(message)
                used += tokens

        selected_recent: list[dict[str, Any]] = []
        for message in reversed(conversation):
            tokens = self.estimate_tokens(message)
            if used + tokens <= available:
                selected_recent.append(message)
                used += tokens
                continue
            if not selected_recent:
                # Always preserve at least the newest message, truncating from
                # its beginning if a single message exceeds the whole budget.
                kept_chars = max(1, (available - used - 4) * 4)
                truncated = dict(message)
                content = str(message.get("content", ""))
                truncated["content"] = content[-kept_chars:]
                selected_recent.append(truncated)
            break
        selected_recent.reverse()
        # Token budgeting can trim either side of the prefix/recent split
        # independently, which may separate an assistant's tool_calls[] from
        # its tool result (or vice versa) even though compact_context already
        # protected the stored history — sanitize the final selection too.
        return drop_orphan_tool_pairs(selected_prefix + selected_recent)

    def _context_hierarchy(self) -> ContextHierarchy:
        return ContextHierarchy(self.db, graph=self.graph)

    def _canonical_messages(
        self,
        db_session: SessionModel,
        project_id: str | None = None,
        current_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._context_hierarchy().build_messages(
            db_session, project_id=project_id, current_turn_id=current_turn_id
        )

    @staticmethod
    def completed_turn(
        db_session: SessionModel,
        turn_id: str,
    ) -> dict[str, Any] | None:
        for message in reversed(list(db_session.messages or [])):
            if (
                message.get("turn_id") == turn_id
                and message.get("role") == "assistant"
                and message.get("status", "complete") == "complete"
                and not message.get("tool_calls")
            ):
                return message
        return None

    def ensure_user_message(
        self,
        db_session: SessionModel,
        content: str,
        turn_id: str,
    ) -> None:
        existing = next(
            (
                message
                for message in list(db_session.messages or [])
                if message.get("turn_id") == turn_id
                and message.get("role") == "user"
            ),
            None,
        )
        if existing is not None:
            if existing.get("content") != content:
                raise ValueError(
                    "Idempotency key was already used with a different message"
                )
            return
        self.append_message(
            db_session,
            role="user",
            content=content,
            turn_id=turn_id,
            idempotency_key=turn_id,
            status="complete",
        )

    @staticmethod
    def _tool_arguments(tool_call: Mapping[str, Any]) -> Mapping[str, Any]:
        """Decode the normalized adapter input while tolerating bad JSON."""

        arguments = tool_call.get("input", {})
        if isinstance(arguments, Mapping):
            return arguments
        if isinstance(arguments, str):
            try:
                decoded = json.loads(arguments)
            except json.JSONDecodeError:
                return {}
            return decoded if isinstance(decoded, Mapping) else {}
        return {}

    async def _execute_tools(
        self,
        tool_calls: list[dict[str, Any]],
        db_session: SessionModel,
        active_tool_names: set[str],
    ) -> list[dict[str, Any]]:
        """Execute normalized adapter tool calls and return OpenAI messages.

        Deferred tools not yet loaded into ``active_tool_names`` for this
        turn (via ``load_tools``) are rejected with a guiding error instead
        of executed, so a model that calls one before loading its group
        gets a recoverable message rather than a crashed loop.
        """

        router = CommandRouter(self.db)
        results: list[dict[str, Any]] = []
        for position, tool_call in enumerate(tool_calls):
            name = str(tool_call.get("name", ""))
            call_id = str(tool_call.get("id") or f"tool-call-{position}")
            canonical_name = resolve_tool_name(name)
            spec = get_spec(canonical_name)
            if spec is not None and spec.tier == "deferred" and canonical_name not in active_tool_names:
                result: dict[str, Any] = {
                    "error": (
                        f"Tool '{name}' is not loaded for this turn. Call "
                        f"load_tools(group=\"{spec.group}\") first, then retry."
                    )
                }
            else:
                result = await router.execute_tool(
                    name,
                    self._tool_arguments(tool_call),
                    db_session.id,
                )
            results.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
        return results

    def _merge_loaded_tools(
        self,
        active_tools: list[dict[str, Any]],
        active_tool_names: set[str],
        tool_calls: list[dict[str, Any]],
    ) -> None:
        """Fold groups requested via ``load_tools`` into this turn's active set.

        Mutates ``active_tools``/``active_tool_names`` in place so a
        ``load_tools`` call and a call to one of its group's tools can land
        in the same tool_calls batch, and so the next loop iteration's
        request carries the expanded set.
        """

        for tool_call in tool_calls:
            if resolve_tool_name(str(tool_call.get("name", ""))) != "load_tools":
                continue
            group = str(self._tool_arguments(tool_call).get("group", "")).strip()
            for schema in get_group_tool_definitions(group) or []:
                if schema["name"] not in active_tool_names:
                    active_tools.append(schema)
                    active_tool_names.add(schema["name"])

    def _persist_tool_exchange(
        self,
        db_session: SessionModel,
        *,
        turn_id: str,
        response: ProviderResponse,
        results: list[dict[str, Any]],
    ) -> None:
        """Persist the assistant call envelope and each tool result."""

        self.append_message(
            db_session,
            role="assistant",
            content=response.text or "",
            turn_id=turn_id,
            status="complete",
            provider=response.provider,
            model=response.model,
            provider_response_id=response.request_id,
            stop_reason=response.stop_reason,
            tool_calls=response.tool_calls or [],
        )
        for result in results:
            self.append_message(
                db_session,
                turn_id=turn_id,
                status="complete",
                **result,
            )
        # Mutating tools can change the structured context snapshot used by
        # the next provider request in this turn and by the next user turn.
        invalidate_context_snapshot(self.db, project_id=db_session.project_id)

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, CLIDispatchError):
            return exc.retryable
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        return status_code in {408, 409, 429} or (
            isinstance(status_code, int) and status_code >= 500
        )

    async def _retry_delay(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        if delay:
            await asyncio.sleep(delay)

    def _persist_success(
        self,
        db_session: SessionModel,
        *,
        turn_id: str,
        response: ProviderResponse,
        latency_ms: int,
        tool_iterations: int = 0,
    ) -> CoordinatorResult:
        assistant = self.append_message(
            db_session,
            role="assistant",
            content=response.text,
            turn_id=turn_id,
            idempotency_key=turn_id,
            status="complete",
            provider=response.provider,
            model=response.model,
            provider_response_id=response.request_id,
            stop_reason=response.stop_reason,
            tool_iterations=tool_iterations,
        )
        db_session.selected_provider = response.provider
        db_session.selected_model = response.model
        self.db.commit()
        self._record_usage(db_session, response, latency_ms)
        logger.info(
            "Coordinator turn completed for session=%s turn_id=%s model=%s iterations=%d latency_ms=%d",
            db_session.id,
            turn_id,
            response.model,
            tool_iterations,
            latency_ms,
        )
        return CoordinatorResult(
            content=response.text,
            message_id=assistant["id"],
            turn_id=turn_id,
            provider=response.provider,
            model=response.model,
        )

    def _record_usage(
        self,
        db_session: SessionModel,
        response: ProviderResponse,
        latency_ms: int,
    ) -> None:
        usage = response.usage or UsageCounts()
        record = LLMUsage(
            session_id=db_session.id,
            task_id=db_session.task_id,
            model=response.model,
            provider=response.provider,
            operation="chat",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            cost_usd=calculate_cost(
                response.model,
                response.provider,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
            ),
            latency_ms=max(0, latency_ms),
        )
        try:
            self.db.add(record)
            self.db.commit()
        except Exception:
            self.db.rollback()
            logger.exception(
                "Failed to record coordinator usage for session=%s model=%s",
                db_session.id,
                response.model,
            )

    def _persist_failure(
        self,
        db_session: SessionModel,
        *,
        turn_id: str,
        provider: str,
        model: str,
        partial: str,
        error: Exception,
    ) -> None:
        self.append_message(
            db_session,
            role="assistant",
            content=partial,
            turn_id=turn_id,
            idempotency_key=turn_id,
            status="failed",
            provider=provider,
            model=model,
            error=str(error),
        )

    def _cached_result(
        self,
        message: dict[str, Any],
        turn_id: str,
    ) -> CoordinatorResult:
        return CoordinatorResult(
            content=str(message.get("content", "")),
            message_id=str(message.get("id", "")),
            turn_id=turn_id,
            provider=str(message.get("provider", "")),
            model=str(message.get("model", "")),
            cached=True,
        )

    @staticmethod
    def format_prompt(messages: list[dict[str, Any]]) -> str:
        """Expose the canonical-to-CLI prompt conversion for callers/tests."""

        return format_history_as_prompt(messages)

    def _cli_response(
        self,
        *,
        provider: str,
        model: str,
        content: str,
        canonical: list[dict[str, Any]],
    ) -> ProviderResponse:
        """Normalize CLI output to the existing persistence response shape."""

        return ProviderResponse(
            provider=provider,
            model=model,
            text=content,
            usage=UsageCounts(
                input_tokens=sum(self.estimate_tokens(item) for item in canonical),
                output_tokens=max(0, self.estimate_tokens({"content": content}) - 4),
                cached_tokens=0,
            ),
            request_id=f"cli-{uuid.uuid4()}",
            stop_reason="stop",
        )

    async def _complete_cli(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        canonical: list[dict[str, Any]],
    ) -> ProviderResponse:
        route = route_model(model, provider)
        chunks: list[str] = []
        async for chunk in self.dispatcher.spawn(route.cli, model, prompt):
            chunks.append(chunk)
        return self._cli_response(
            provider=provider,
            model=model,
            content="".join(chunks),
            canonical=canonical,
        )

    async def complete_turn(
        self,
        db_session: SessionModel,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        idempotency_key: str | None = None,
        temperature: float = 0.7,
    ) -> CoordinatorResult:
        """Complete and persist one non-streaming coordinator turn."""

        turn_id = idempotency_key or str(uuid.uuid4())
        lock = self._session_locks.setdefault(db_session.id, asyncio.Lock())
        async with lock:
            # The ORM object may have been loaded before another request
            # finished waiting on this lock.
            self.db.refresh(db_session)
            self.ensure_user_message(db_session, message, turn_id)
            completed = self.completed_turn(db_session, turn_id)
            if completed:
                return self._cached_result(completed, turn_id)
            provider_name, resolved_model, adapter = self._resolve_selection(
                db_session, model, provider
            )
            ctx = self._context_hierarchy()
            ctx.compact_context(
                db_session,
                model=resolved_model,
                context_window=self._context_window(resolved_model),
            )
            canonical = self.budget_messages(
                ctx.build_messages(db_session, current_turn_id=turn_id),
                resolved_model,
            )
            prompt = self.format_prompt(canonical)
            started = perf_counter()
            tool_activity = False
            active_tools = ctx.get_tool_definitions()
            active_tool_names = {tool["name"] for tool in active_tools}
            for attempt in range(self.max_retries + 1):
                try:
                    if adapter is not None:
                        iteration = 0
                        accumulated_tokens = 0
                        executed_tool_calls_history: list[str] = []
                        last_tool_sig: tuple[str, str] | None = None
                        consecutive_repeat_count = 0
                        stop_reason: str | None = None
                        stop_message: str | None = None
                        last_response: ProviderResponse | None = None

                        while iteration < self.max_tool_iterations:
                            iteration += 1
                            response = await adapter.complete(
                                canonical,
                                resolved_model,
                                False,
                                max_tokens=self.max_output_tokens,
                                temperature=temperature,
                                tools=active_tools,
                            )
                            last_response = response
                            if response.usage:
                                accumulated_tokens += (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)

                            if not response.tool_calls:
                                return self._persist_success(
                                    db_session,
                                    turn_id=turn_id,
                                    response=response,
                                    latency_ms=round((perf_counter() - started) * 1000),
                                    tool_iterations=iteration,
                                )

                            tool_activity = True

                            for tool_call in response.tool_calls:
                                name = str(tool_call.get("name", ""))
                                canonical_name = resolve_tool_name(name)
                                args = self._tool_arguments(tool_call)
                                args_str = json.dumps(args, sort_keys=True)
                                sig = (canonical_name, args_str)

                                if sig == last_tool_sig:
                                    consecutive_repeat_count += 1
                                else:
                                    last_tool_sig = sig
                                    consecutive_repeat_count = 1

                                executed_tool_calls_history.append(canonical_name)

                                if consecutive_repeat_count >= self.max_repeated_tool_calls:
                                    stop_reason = "repeated_tool_call"
                                    tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                                    stop_message = (
                                        f"Turn stopped early: detected repeated call to tool '{canonical_name}' "
                                        f"with identical arguments ({consecutive_repeat_count} times in a row). "
                                        f"Completed tool calls: [{tools_summary}]. "
                                        f"Please check inputs or refine instructions to continue."
                                    )
                                    break

                            canonical.append(
                                {
                                    "role": "assistant",
                                    "content": response.text or "",
                                    "tool_calls": response.tool_calls,
                                }
                            )
                            self._merge_loaded_tools(
                                active_tools, active_tool_names, response.tool_calls
                            )
                            tool_results = await self._execute_tools(
                                response.tool_calls,
                                db_session,
                                active_tool_names,
                            )
                            canonical.extend(tool_results)
                            self._persist_tool_exchange(
                                db_session,
                                turn_id=turn_id,
                                response=response,
                                results=tool_results,
                            )

                            if stop_reason:
                                break

                            if accumulated_tokens >= self.max_turn_tokens:
                                stop_reason = "token_budget_exceeded"
                                tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                                stop_message = (
                                    f"Turn reached maximum token budget ({accumulated_tokens} >= {self.max_turn_tokens} tokens). "
                                    f"Completed tool calls: [{tools_summary}]. "
                                    f"You can reply to continue from where it stopped."
                                )
                                break

                        if not stop_reason and iteration >= self.max_tool_iterations:
                            stop_reason = "max_iterations_exceeded"
                            tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                            stop_message = (
                                f"Turn reached maximum tool iteration limit ({self.max_tool_iterations} iterations). "
                                f"Completed tool calls: [{tools_summary}]. "
                                f"You can reply to continue from where it stopped."
                            )

                        if stop_reason and stop_message:
                            soft_response = ProviderResponse(
                                provider=last_response.provider if last_response else provider_name,
                                model=last_response.model if last_response else resolved_model,
                                text=stop_message,
                                usage=last_response.usage if last_response else None,
                                request_id=last_response.request_id if last_response else f"soft-stop-{uuid.uuid4()}",
                                stop_reason=stop_reason,
                            )
                            return self._persist_success(
                                db_session,
                                turn_id=turn_id,
                                response=soft_response,
                                latency_ms=round((perf_counter() - started) * 1000),
                                tool_iterations=iteration,
                            )
                    else:
                        response = await self._complete_cli(
                            provider=provider_name,
                            model=resolved_model,
                            prompt=prompt,
                            canonical=canonical,
                        )
                    return self._persist_success(
                        db_session,
                        turn_id=turn_id,
                        response=response,
                        latency_ms=round((perf_counter() - started) * 1000),
                    )
                except Exception as exc:
                    if (
                        not tool_activity
                        and attempt < self.max_retries
                        and self._retryable(exc)
                    ):
                        await self._retry_delay(attempt)
                        continue
                    self._persist_failure(
                        db_session,
                        turn_id=turn_id,
                        provider=provider_name,
                        model=resolved_model,
                        partial="",
                        error=exc,
                    )
                    raise
        raise RuntimeError("Coordinator turn ended without a result")

    async def stream_turn(
        self,
        db_session: SessionModel,
        message: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        idempotency_key: str | None = None,
        temperature: float = 0.7,
    ) -> AsyncIterator[str | dict[str, Any]]:
        """Stream text and tool progress, then persist one logical response."""

        turn_id = idempotency_key or str(uuid.uuid4())
        lock = self._session_locks.setdefault(db_session.id, asyncio.Lock())
        async with lock:
            self.db.refresh(db_session)
            self.ensure_user_message(db_session, message, turn_id)
            completed = self.completed_turn(db_session, turn_id)
            if completed:
                yield str(completed.get("content", ""))
                return
            provider_name, resolved_model, adapter = self._resolve_selection(
                db_session, model, provider
            )
            ctx = self._context_hierarchy()
            ctx.compact_context(
                db_session,
                model=resolved_model,
                context_window=self._context_window(resolved_model),
            )
            canonical = self.budget_messages(
                ctx.build_messages(db_session, current_turn_id=turn_id),
                resolved_model,
            )
            prompt = self.format_prompt(canonical)
            started = perf_counter()
            partial = ""
            tool_activity = False
            active_tools = ctx.get_tool_definitions()
            active_tool_names = {tool["name"] for tool in active_tools}
            for attempt in range(self.max_retries + 1):
                response: ProviderResponse | None = None
                try:
                    if adapter is not None:
                        iteration = 0
                        accumulated_tokens = 0
                        executed_tool_calls_history: list[str] = []
                        last_tool_sig: tuple[str, str] | None = None
                        consecutive_repeat_count = 0
                        stop_reason: str | None = None
                        stop_message: str | None = None

                        while iteration < self.max_tool_iterations:
                            iteration += 1
                            response = await adapter.complete(
                                canonical,
                                resolved_model,
                                True,
                                max_tokens=self.max_output_tokens,
                                temperature=temperature,
                                tools=active_tools,
                            )
                            if response.usage:
                                accumulated_tokens += (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)

                            if response.chunks is None:
                                if response.text:
                                    partial += response.text
                                    yield response.text
                            else:
                                async for chunk in response.chunks:
                                    partial += chunk
                                    yield chunk

                            if not response.tool_calls:
                                break

                            tool_activity = True

                            for tool_call in response.tool_calls:
                                name = str(tool_call.get("name", ""))
                                canonical_name = resolve_tool_name(name)
                                args = self._tool_arguments(tool_call)
                                args_str = json.dumps(args, sort_keys=True)
                                sig = (canonical_name, args_str)

                                if sig == last_tool_sig:
                                    consecutive_repeat_count += 1
                                else:
                                    last_tool_sig = sig
                                    consecutive_repeat_count = 1

                                executed_tool_calls_history.append(canonical_name)

                                if consecutive_repeat_count >= self.max_repeated_tool_calls:
                                    stop_reason = "repeated_tool_call"
                                    tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                                    stop_message = (
                                        f"Turn stopped early: detected repeated call to tool '{canonical_name}' "
                                        f"with identical arguments ({consecutive_repeat_count} times in a row). "
                                        f"Completed tool calls: [{tools_summary}]. "
                                        f"Please check inputs or refine instructions to continue."
                                    )
                                    break

                            canonical.append(
                                {
                                    "role": "assistant",
                                    "content": response.text or "",
                                    "tool_calls": response.tool_calls,
                                }
                            )
                            self._merge_loaded_tools(
                                active_tools, active_tool_names, response.tool_calls
                            )
                            for position, tool_call in enumerate(response.tool_calls):
                                yield {
                                    "type": "tool_call",
                                    "tool_call_id": str(
                                        tool_call.get("id") or f"tool-call-{position}"
                                    ),
                                    "name": str(tool_call.get("name", "")),
                                    "input": dict(self._tool_arguments(tool_call)),
                                }
                            tool_results = await self._execute_tools(
                                response.tool_calls,
                                db_session,
                                active_tool_names,
                            )
                            canonical.extend(tool_results)
                            self._persist_tool_exchange(
                                db_session,
                                turn_id=turn_id,
                                response=response,
                                results=tool_results,
                            )
                            for result in tool_results:
                                yield {
                                    "type": "tool_result",
                                    "tool_call_id": result["tool_call_id"],
                                    "name": result["name"],
                                    "content": result["content"],
                                }

                            if stop_reason:
                                break

                            if accumulated_tokens >= self.max_turn_tokens:
                                stop_reason = "token_budget_exceeded"
                                tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                                stop_message = (
                                    f"Turn reached maximum token budget ({accumulated_tokens} >= {self.max_turn_tokens} tokens). "
                                    f"Completed tool calls: [{tools_summary}]. "
                                    f"You can reply to continue from where it stopped."
                                )
                                break

                        if not stop_reason and iteration >= self.max_tool_iterations and response and response.tool_calls:
                            stop_reason = "max_iterations_exceeded"
                            tools_summary = ", ".join(f"'{t}'" for t in executed_tool_calls_history)
                            stop_message = (
                                f"Turn reached maximum tool iteration limit ({self.max_tool_iterations} iterations). "
                                f"Completed tool calls: [{tools_summary}]. "
                                f"You can reply to continue from where it stopped."
                            )

                        if stop_reason and stop_message:
                            partial += stop_message
                            yield stop_message
                            response = ProviderResponse(
                                provider=response.provider if response else provider_name,
                                model=response.model if response else resolved_model,
                                text=stop_message,
                                usage=response.usage if response else None,
                                request_id=response.request_id if response else f"soft-stop-{uuid.uuid4()}",
                                stop_reason=stop_reason,
                            )
                    else:
                        route = route_model(resolved_model, provider_name)
                        async for chunk in self.dispatcher.spawn(
                            route.cli,
                            resolved_model,
                            prompt,
                        ):
                            partial += chunk
                            yield chunk
                        response = self._cli_response(
                            provider=provider_name,
                            model=resolved_model,
                            content=partial,
                            canonical=canonical,
                        )
                    self._persist_success(
                        db_session,
                        turn_id=turn_id,
                        response=response,
                        latency_ms=round((perf_counter() - started) * 1000),
                        tool_iterations=iteration if adapter is not None else 1,
                    )
                    return
                except Exception as exc:
                    # Retrying after emitting a delta would duplicate visible
                    # output, so retries are safe only before the first chunk.
                    if (
                        not partial
                        and not tool_activity
                        and attempt < self.max_retries
                        and self._retryable(exc)
                    ):
                        await self._retry_delay(attempt)
                        continue
                    self._persist_failure(
                        db_session,
                        turn_id=turn_id,
                        provider=provider_name,
                        model=resolved_model,
                        partial=partial,
                        error=exc,
                    )
                    raise
