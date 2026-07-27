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
from app.services.context_hierarchy import ContextHierarchy
from app.services.crypto import decrypt_api_key
from app.services.command_router import CommandRouter
from app.graph.context import invalidate_context_snapshot
from app.services.providers import CoordinatorProvider, ProviderResponse
from app.services.providers.anthropic_adapter import AnthropicAdapter
from app.services.providers.google_adapter import GoogleAdapter
from app.services.providers.openai_adapter import OpenAIAdapter


logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOWS = {
    "claude": 200_000,
    "gemini": 1_000_000,
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
    """Resolve coordinator adapters from explicit provider or model name."""

    def __init__(
        self,
        providers: Mapping[str, CoordinatorProvider] | None = None,
    ):
        self.providers: dict[str, CoordinatorProvider] = dict(
            providers
            if providers is not None
            else {
                "anthropic": AnthropicAdapter(),
                "google": GoogleAdapter(),
            }
        )

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
            supported = ", ".join(sorted(self.providers))
            raise ValueError(
                f"Unsupported coordinator provider '{provider_name}'. "
                f"Supported providers: {supported}"
            ) from exc

    # Friendly alias for call sites and tests.
    route = get


DEFAULT_PROVIDER_ROUTER = ProviderRouter()


class CoordinatorService:
    """Own coordinator sessions, CLI dispatch, retries, and telemetry.

    The optional provider arguments remain a small compatibility seam for
    existing callers/tests.  Normal application construction uses the CLI
    dispatcher and therefore never instantiates an SDK request.
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
        max_tool_iterations: int = 5,
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
        self._explicit_provider_compatibility = router is not None or providers is not None
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
        self.max_tool_iterations = max(1, max_tool_iterations)

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

        agent: AgentModel | None = None
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

        # SDK adapters are supported only for the legacy injected-provider
        # path.  The normal path returns None and always dispatches through a
        # CLI account login.
        legacy_adapter: CoordinatorProvider | None = None
        if self._explicit_provider_compatibility:
            legacy_adapter = self.router.get(
                requested_model,
                provider,
                agent=agent,
            )
        else:
            if provider == "openai":
                legacy_adapter = self.router.get(
                    requested_model,
                    provider,
                    agent=agent,
                )
            else:
                configured = self.router.providers.get(provider)
                if configured is not None and not isinstance(
                    configured,
                    (AnthropicAdapter, GoogleAdapter),
                ):
                    legacy_adapter = configured
        return provider, requested_model, legacy_adapter

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
        prefix = [
            message
            for message in messages
            if message.get("role") == "system" or message.get("pinned")
        ]
        conversation = [
            message
            for message in messages
            if message not in prefix
            and message.get("status", "complete") == "complete"
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
        return selected_prefix + selected_recent

    def _context_hierarchy(self) -> ContextHierarchy:
        return ContextHierarchy(self.db, graph=self.graph)

    def _canonical_messages(
        self,
        db_session: SessionModel,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._context_hierarchy().build_messages(db_session, project_id=project_id)

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
    ) -> list[dict[str, Any]]:
        """Execute normalized adapter tool calls and return OpenAI messages."""

        router = CommandRouter(self.db)
        results: list[dict[str, Any]] = []
        for position, tool_call in enumerate(tool_calls):
            name = str(tool_call.get("name", ""))
            call_id = str(tool_call.get("id") or f"tool-call-{position}")
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
        )
        db_session.selected_provider = response.provider
        db_session.selected_model = response.model
        self.db.commit()
        self._record_usage(db_session, response, latency_ms)
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
            ctx.compact_context(db_session)
            canonical = self.budget_messages(
                ctx.build_messages(db_session),
                resolved_model,
            )
            prompt = self.format_prompt(canonical)
            started = perf_counter()
            tool_activity = False
            for attempt in range(self.max_retries + 1):
                try:
                    if adapter is not None:
                        for _ in range(self.max_tool_iterations):
                            response = await adapter.complete(
                                canonical,
                                resolved_model,
                                False,
                                max_tokens=self.max_output_tokens,
                                temperature=temperature,
                                tools=ctx.get_tool_definitions(),
                            )
                            if not response.tool_calls:
                                return self._persist_success(
                                    db_session,
                                    turn_id=turn_id,
                                    response=response,
                                    latency_ms=round((perf_counter() - started) * 1000),
                                )

                            tool_activity = True
                            canonical.append(
                                {
                                    "role": "assistant",
                                    "content": response.text or "",
                                    "tool_calls": response.tool_calls,
                                }
                            )
                            tool_results = await self._execute_tools(
                                response.tool_calls,
                                db_session,
                            )
                            canonical.extend(tool_results)
                            self._persist_tool_exchange(
                                db_session,
                                turn_id=turn_id,
                                response=response,
                                results=tool_results,
                            )
                        raise RuntimeError(
                            "Coordinator tool execution loop exceeded "
                            f"{self.max_tool_iterations} iterations"
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
            ctx.compact_context(db_session)
            canonical = self.budget_messages(
                ctx.build_messages(db_session),
                resolved_model,
            )
            prompt = self.format_prompt(canonical)
            started = perf_counter()
            partial = ""
            tool_activity = False
            for attempt in range(self.max_retries + 1):
                response: ProviderResponse | None = None
                try:
                    if adapter is not None:
                        for _ in range(self.max_tool_iterations):
                            response = await adapter.complete(
                                canonical,
                                resolved_model,
                                True,
                                max_tokens=self.max_output_tokens,
                                temperature=temperature,
                                tools=ctx.get_tool_definitions(),
                            )
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
                            canonical.append(
                                {
                                    "role": "assistant",
                                    "content": response.text or "",
                                    "tool_calls": response.tool_calls,
                                }
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
                        else:
                            raise RuntimeError(
                                "Coordinator tool execution loop exceeded "
                                f"{self.max_tool_iterations} iterations"
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
