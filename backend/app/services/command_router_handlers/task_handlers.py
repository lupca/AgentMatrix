import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple
from collections.abc import Mapping

from app.db.models import (
    Agent,
    AgentOutputChunk,
    AgentRun,
    AuditLog,
    GateRecord,
    Project,
    ReviewCycle,
    Session as SessionModel,
    Task,
    TaskDependency,
    TaskEvent,
)
from app.services.archive import ArchiveError, ArchiveService
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
)
from app.services.task_state_machine import find_active_plan_run
from app.services.task_validators import TaskValidator, TransitionConflictError

logger = logging.getLogger(__name__)


class TaskHandlersMixin:
    def _orchestration_error_payload(
        self, exc: OrchestrationError, task_id: str | None
    ) -> dict[str, Any]:
        """Turn a rejected orchestration call into {error, next_step}.

        A bare "expected status X, found Y" tells the caller nothing about
        how to get unstuck (CTV2-1394). When the failure is a state-conflict
        and we can still find the task, attach TaskValidator.describe_next_step
        so the tool name to call next is right there in the error.
        """
        payload: dict[str, Any] = {'error': str(exc)}
        if isinstance(exc, TransitionConflictError) and task_id:
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if task is not None:
                payload['next_step'] = TaskValidator(self.db).describe_next_step(task)
        return payload

    def _task_snapshot(self, task: Task) -> dict[str, Any]:
        return {
            'id': task.id,
            'status': task.status,
            'current_gate': task.current_gate,
            'awaiting_approval': bool(task.awaiting_approval),
            'approval_prompt': task.approval_prompt,
            'executor': task.executor,
            'reviewer': task.reviewer,
            'result_ref': task.result_ref,
            'landed_ref': task.landed_ref,
            'error': task.error,
            'spec_clarity': task.spec_clarity,
            'open_questions': task.open_questions or [],
        }

    def _open_questions_status(self, task: Task) -> dict[str, Any]:
        """spec_clarity/open_questions plus whether they're stale (CTV2-1396).

        These columns are only overwritten when a plan run finishes. If a
        plan run is currently queued/running, the values on `task` still
        reflect the PREVIOUS round -- surfacing them without saying so leads
        the coordinator to conclude the planner ignored an answer and
        re-answer the same questions, burning an extra planner round. We
        keep the old values (don't erase in case the new run fails) but
        attach {state, why, next} so one read is enough to know not to act.
        """
        result: dict[str, Any] = {
            'spec_clarity': task.spec_clarity,
            'open_questions': task.open_questions or [],
        }
        active_run = find_active_plan_run(self.db, task.id)
        if active_run is not None and (task.open_questions or task.spec_clarity):
            started = active_run.started_at or active_run.queued_at
            result['open_questions_status'] = {
                'state': 'stale_previous_round',
                'why': (
                    f"plan run {active_run.id} đang chạy (khởi động {started}); "
                    "open_questions/spec_clarity ở trên thuộc vòng trước run này."
                ),
                'next': (
                    "Đừng trả lời lại các câu hỏi trên -- chờ plan run "
                    f"{active_run.id} chạy xong, kết quả mới sẽ ghi đè."
                ),
                'active_run_id': active_run.id,
            }
        return result

    def _pending_gate(self, task_id: str) -> GateRecord | None:
        decided_parent_ids = (
            self.db.query(GateRecord.parent_id)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.parent_id.isnot(None),
            )
            .scalar_subquery()
        )

        return (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.status == "pending",
                GateRecord.id.notin_(decided_parent_ids),
            )
            .order_by(GateRecord.id.desc())
            .first()
        )

    def _dispatch_attempt(self, task_id: str) -> int:
        existing_runs = (
            self.db.query(AgentRun).filter(AgentRun.task_id == task_id).count()
        )
        return existing_runs + 1

    def _approve_attempt(self, task_id: str) -> int:
        existing_decisions = (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.status.in_(["approved", "rejected"]),
            )
            .count()
        )
        return existing_decisions + 1

    async def _handle_get_run_output(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
            task_id = str(payload.get('task_id', '')).strip()
            run_id = str(payload.get('run_id', '')).strip()
            offset = max(0, int(payload.get('offset', 0)))
            limit = min(100, max(1, int(payload.get('limit', 20))))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid get_run_output arguments'}
        if not task_id or not run_id:
            return {'error': 'task_id and run_id are required'}
        run = self.db.query(AgentRun).filter(
            AgentRun.id == run_id, AgentRun.task_id == task_id
        ).first()
        if run is None:
            return {'error': f'Run {run_id} not found for task {task_id}'}
        chunks = (
            self.db.query(AgentOutputChunk)
            .filter(AgentOutputChunk.run_id == run_id)
            .order_by(AgentOutputChunk.chunk_index.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            'task_id': task_id,
            'run_id': run_id,
            'status': run.status,
            'offset': offset,
            'limit': limit,
            'chunks': [
                {'index': chunk.chunk_index, 'content': chunk.content,
                 'timestamp': chunk.timestamp.isoformat() if chunk.timestamp else None}
                for chunk in chunks
            ],
            'has_more': len(chunks) == limit,
        }

    async def _handle_get_task_events(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
            since_id = int(payload.get('since_id') or 0)
            limit = min(200, max(1, int(payload.get('limit') or 50)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid get_task_events arguments'}
        task_id = str(payload.get('task_id') or '').strip() or None
        kind = str(payload.get('kind') or '').strip() or None
        event_types = payload.get('event_types') or None

        query = self.db.query(TaskEvent)
        if task_id:
            query = query.filter(TaskEvent.task_id == task_id)
        if since_id:
            query = query.filter(TaskEvent.id > since_id)
        if kind:
            query = query.filter(TaskEvent.kind == kind)
        if event_types:
            query = query.filter(TaskEvent.event_type.in_([str(t) for t in event_types]))
        events = query.order_by(TaskEvent.id.asc()).limit(limit).all()
        return {
            'events': [
                {
                    'id': event.id,
                    'task_id': event.task_id,
                    'event_type': event.event_type,
                    'kind': event.kind,
                    'payload': event.payload,
                    'created_at': event.created_at.isoformat() if event.created_at else None,
                }
                for event in events
            ],
            'cursor': events[-1].id if events else since_id,
            'has_more': len(events) == limit,
        }

    async def _handle_wait_for_task(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
            raw_since_event_id = payload.get('since_event_id')
            since_event_id = (
                int(raw_since_event_id) if raw_since_event_id is not None else None
            )
            timeout_seconds = min(120, max(5, int(payload.get('timeout_seconds') or 55)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid wait_for_task arguments'}
        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}

        _TERMINAL = {'done', 'failed', 'cancelled', 'changes-requested'}
        _POLL_INTERVAL = 2.0

        self.db.rollback()
        initial_task = self.db.get(Task, task_id)
        if initial_task is None:
            return {'error': f"Task '{task_id}' not found"}
        effective_cursor = since_event_id
        if effective_cursor is None:
            effective_cursor = (
                self.db.query(TaskEvent.id)
                .filter(TaskEvent.task_id == task_id)
                .order_by(TaskEvent.id.desc())
                .limit(1)
                .scalar()
                or 0
            )
        initial_status = initial_task.status

        def _snapshot() -> tuple[dict | None, list[dict], int]:
            self.db.rollback()
            task = self.db.get(Task, task_id)
            if task is None:
                return None, [], effective_cursor
            events = (
                self.db.query(TaskEvent)
                .filter(TaskEvent.task_id == task_id, TaskEvent.id > effective_cursor)
                .order_by(TaskEvent.id.asc())
                .limit(20)
                .all()
            )
            cursor = events[-1].id if events else effective_cursor
            task_dict = {
                'id': task.id,
                'status': task.status,
                'executor': task.executor,
                'reviewer': task.reviewer,
                'result_ref': task.result_ref,
                'error': task.error,
                'awaiting_approval': bool(task.awaiting_approval),
                'approval_prompt': task.approval_prompt,
            }
            event_dicts = [
                {'id': e.id, 'event_type': e.event_type, 'kind': e.kind,
                 'payload': e.payload,
                 'created_at': e.created_at.isoformat() if e.created_at else None}
                for e in events
            ]
            return task_dict, event_dicts, cursor

        waited = 0.0
        while True:
            task_dict, event_dicts, cursor = _snapshot()
            if task_dict is None:
                return {'error': f"Task '{task_id}' not found"}
            changed = (
                task_dict['status'] != initial_status
                or task_dict['status'] in _TERMINAL
                or task_dict['awaiting_approval']
                or bool(event_dicts)
            )
            if changed or waited >= timeout_seconds:
                latest_run = (
                    self.db.query(AgentRun)
                    .filter(AgentRun.task_id == task_id)
                    .order_by(AgentRun.queued_at.desc())
                    .first()
                )
                return {
                    'task': task_dict,
                    'changed': changed,
                    'events': event_dicts,
                    'cursor': cursor,
                    'waited_seconds': round(waited),
                    'latest_run': {
                        'id': latest_run.id,
                        'kind': latest_run.kind,
                        'agent_id': latest_run.agent_id,
                        'status': latest_run.status,
                        'result_ref': latest_run.result_ref,
                        'error_message': latest_run.error_message,
                    } if latest_run else None,
                }
            await asyncio.sleep(_POLL_INTERVAL)
            waited += _POLL_INTERVAL

    async def _handle_archive_task(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid archive_task arguments'}
        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}
        service = ArchiveService(self.db, actor=f"chat:{session_id or 'anonymous'}")
        try:
            if payload.get('restore'):
                return service.restore('tasks', task_id)
            return service.archive('tasks', task_id)
        except ArchiveError as exc:
            return {'error': str(exc)}

    async def _handle_create_task(self, args: str, session_id: str) -> dict:
        from sqlalchemy import update as sa_update

        # Two encodings: JSON from the MCP tool surface, and the legacy flag
        # string from the `/pm <title> --project p` slash alias. The flag form
        # cannot express a multi-line description, so the tool surface moved to
        # JSON; the string form is kept so typed commands keep working.
        depends_on: list[str] = []
        description = ''
        project = None
        title = args

        payload = None
        if args.strip().startswith('{'):
            try:
                candidate = json.loads(args)
                if isinstance(candidate, dict):
                    payload = candidate
            except json.JSONDecodeError:
                payload = None

        if payload is not None:
            title = str(payload.get('title', '')).strip()
            project = str(payload.get('project', '')).strip() or None
            description = str(payload.get('description', '') or '')
            depends_on = [str(d) for d in (payload.get('depends_on') or []) if d]
        else:
            if '--depends-on' in args:
                args, dep_part = args.split('--depends-on', 1)
                dep_part = dep_part.strip().split()[0] if dep_part.strip() else ''
                depends_on = [dep_id for dep_id in dep_part.split(',') if dep_id]

            title = args
            if '--project' in args:
                parts = args.split('--project')
                title = parts[0].strip()
                explicit_project = parts[1].strip().split()[0] if parts[1].strip() else ''
                project = explicit_project or None

        if not project:
            session = (
                self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            )
            if session and session.project_id:
                project = session.project_id

        if not project:
            return {
                'action': 'error',
                'error': 'project_required',
                'message': (
                    'Cannot determine project for this task. Pass --project <id> '
                    'or use a project-scoped session.'
                ),
            }

        update_result = self.db.execute(
            sa_update(Project)
            .where(Project.id == project)
            .values(next_task_seq=Project.next_task_seq + 1)
        )
        if update_result.rowcount == 0:
            self.db.rollback()
            return {
                'action': 'error',
                'error': 'unknown_project',
                'message': f"Project '{project}' does not exist.",
            }

        project_row = self.db.query(Project).filter(Project.id == project).first()
        seq = project_row.next_task_seq
        prefix = (project_row.task_prefix or project).upper().replace('-', '')[:4]
        task_id = f'{prefix}-{seq:03d}'

        now = datetime.now(timezone.utc)
        task = Task(
            id=task_id,
            title=title,
            project=project,
            # raw_input is the field the planner actually reads (_build_prompt
            # in spec_plan_generator). A task created without it has nothing but
            # a title to plan from, and the fail-closed dispatch check will
            # refuse it — which is exactly how CTV2-1380 ended up unrecoverable.
            raw_input=description or None,
            status='todo',
            current_gate='spec',
            created_at=now,
            updated_at=now,
        )
        task.mode = TaskOrchestrationService(self.db).mode_for_task(task)
        self.db.add(task)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        if depends_on:
            service = TaskOrchestrationService(self.db)
            for dep_id in depends_on:
                try:
                    service.add_dependency(
                        task_id=task.id,
                        depends_on_task_id=dep_id,
                        actor=f"chat:{session_id or 'anonymous'}",
                    )
                except OrchestrationError as exc:
                    return {
                        'action': 'error',
                        'error': str(exc),
                        'task_id': task.id,
                    }

        return {
            'action': 'created',
            'task_id': task.id,
            'title': title,
            'project': project,
            'depends_on': depends_on,
        }

    async def _handle_dispatch_task(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import run_agent
        from app.services.agent_matcher import AgentMatcher

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /dispatch <task_id> [agent_id] [--effort <level>]'}

        effort = None
        if '--effort' in parts:
            flag_index = parts.index('--effort')
            if flag_index + 1 >= len(parts):
                return {'error': 'Usage: /dispatch <task_id> [agent_id] [--effort <level>]'}
            effort = parts[flag_index + 1]
            del parts[flag_index:flag_index + 2]

        task_id = parts[0]
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        agent_id = parts[1] if len(parts) > 1 else task.executor
        if not agent_id:
            suggestions = AgentMatcher(self.db).suggest_agents(task, top_n=1)
            agent_id = suggestions[0].agent_id if suggestions else None
        if not agent_id:
            return {'error': 'No available agent found for this task'}
        agent = self.db.query(Agent).filter(Agent.id == agent_id).first()
        if not agent:
            return {'error': f'Agent {agent_id} not found'}
        
        service = TaskOrchestrationService(self.db)
        try:
            result = service.request_dispatch(
                task_id=task_id,
                agent_id=agent_id,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(
                    session_id,
                    "dispatch",
                    args,
                    attempt=self._dispatch_attempt(task_id),
                ),
                effort=effort,
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)

        if not result.applied:
            return {
                'action': 'dispatch_pending',
                'task_id': task_id,
                'gate_record_id': result.gate_record.id,
                'status': 'pending',
                'task': self._task_snapshot(result.task),
            }

        run = result.agent_run
        context = result.context or {}
        if run is None:
            return {'error': 'Dispatch transition did not create an agent run'}
        try:
            message = run_agent.send(
                run.id,
                task_id,
                run.command,
                context['repo_root'],
                run.timeout_seconds,
            )
            run.dramatiq_message_id = str(message.message_id)
            self.db.commit()
        except Exception as exc:
            error = f'Could not queue run: {exc}'
            service.record_dispatch_queue_failure(
                run_id=run.id,
                error=error,
                actor='system:dispatch-queue',
                idempotency_key=f'{result.gate_record.idempotency_key}:queue-failure',
            )
            self.db.refresh(task)
            return {'error': error, 'run_id': run.id, 'task': self._task_snapshot(task)}
        
        self.db.refresh(task)
        return {'action': 'dispatched', 'task_id': task_id, 'run_id': run.id, 'agent': agent_id,
                'task': self._task_snapshot(task)}

    async def _handle_request_review(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import run_agent
        from app.services.agent_matcher import AgentMatcher

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /request-review <task_id> [reviewer]'}

        task_id = parts[0]
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        reviewer = parts[1] if len(parts) > 1 else None
        selection_reason = None
        if not reviewer:
            suggestions = AgentMatcher(self.db).suggest_agents(
                task, top_n=1, exclude_agent_id=task.executor
            )
            if not suggestions:
                return {
                    'error': (
                        f'No independent reviewer available for task {task_id} '
                        f'(executor={task.executor!r}); refusing to lower the '
                        'four-eyes bar.'
                    ),
                    'reason': 'no_independent_reviewer',
                }
            reviewer = suggestions[0].agent_id
            selection_reason = (
                f"{reviewer} selected by matcher: {suggestions[0].reason}"
            )

        service = TaskOrchestrationService(self.db)
        try:
            result = service.request_review(
                task_id=task_id,
                reviewer=reviewer,
                actor=f"chat:{session_id or 'anonymous'}",
                selection_reason=selection_reason,
                idempotency_key=self._command_key(
                    session_id,
                    "request_review",
                    args,
                    attempt=self._dispatch_attempt(task_id),
                ),
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)

        if not result.applied:
            return {
                'action': 'review_pending',
                'task_id': task_id,
                'gate_record_id': result.gate_record.id,
                'status': 'pending',
                'task': self._task_snapshot(result.task),
            }

        run = result.agent_run
        context = result.context or {}
        if run is None:
            return {'error': 'Review transition did not create an agent run'}
        try:
            message = run_agent.send(
                run.id,
                task_id,
                run.command,
                context['repo_root'],
                run.timeout_seconds,
            )
            run.dramatiq_message_id = str(message.message_id)
            self.db.commit()
        except Exception as exc:
            error = f'Could not queue review run: {exc}'
            service.record_dispatch_queue_failure(
                run_id=run.id,
                error=error,
                actor='system:dispatch-queue',
                idempotency_key=f'{result.gate_record.idempotency_key}:queue-failure',
            )
            self.db.refresh(task)
            return {'error': error, 'run_id': run.id, 'task': self._task_snapshot(task)}

        return {
            'action': 'review_requested',
            'task_id': task_id,
            'run_id': run.id,
            'reviewer': reviewer,
            'task': self._task_snapshot(task),
        }

    async def _handle_land_task(self, args: str, session_id: str) -> dict:
        task_id = args.strip()
        if not task_id:
            return {'error': 'Usage: land_task <task_id>'}
        service = TaskOrchestrationService(self.db)
        try:
            return service.land_task(
                task_id=task_id, actor=f"chat:{session_id or 'anonymous'}"
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)

    async def _handle_attach_result(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args and args.strip().startswith('{') else None
        except json.JSONDecodeError:
            payload = None

        if payload and isinstance(payload, dict):
            task_id = str(payload.get('task_id', '')).strip()
            commit = str(payload.get('commit') or payload.get('result_ref') or '').strip()
            option = str(payload.get('option', 'request_review')).strip()
        else:
            parts = args.strip().split(maxsplit=2)
            if not parts:
                return {'error': 'Usage: /attach-result <task_id> <commit> [option]'}
            task_id = parts[0]
            commit = parts[1] if len(parts) > 1 else ''
            option = parts[2] if len(parts) > 2 else 'request_review'

        if not task_id or not commit:
            return {'error': 'task_id and commit are required'}

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        service = TaskOrchestrationService(self.db)
        try:
            result = service.attach_result(
                task_id=task_id,
                commit=commit,
                option=option,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(
                    session_id,
                    "attach_result",
                    args,
                ),
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)

        self.db.refresh(task)
        return {
            'action': 'result_attached',
            'task_id': task_id,
            'commit': task.result_ref,
            'option': option,
            'status': task.status,
            'task': self._task_snapshot(task),
        }

    async def _handle_cancel_task(self, args: str, session_id: str) -> dict:
        from app.workers.output_streamer import publish_status, request_cancel

        task_id = args.strip()
        if not task_id:
            return {'error': 'Usage: /cancel <task_id>'}

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        run = (
            self.db.query(AgentRun)
            .filter(
                AgentRun.task_id == task_id,
                AgentRun.status.in_(['queued', 'running']),
            )
            .first()
        )
        if not run:
            return {'error': f'Task {task_id} has no active run'}

        try:
            request_cancel(run.id, ttl_seconds=max(run.timeout_seconds + 300, 3_600))
        except Exception as exc:
            return {'error': f'Could not signal cancellation: {exc}'}

        try:
            TaskOrchestrationService(self.db).cancel_run(
                run_id=run.id,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(session_id, "cancel", args),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}
        publish_status(run.id, 'cancelled', error=run.error_message)
        self.db.refresh(task)
        return {'action': 'cancelled', 'task_id': task_id, 'run_id': run.id, 'status': 'cancelled',
                'task': self._task_snapshot(task)}

    async def _handle_reopen_task(self, args: str, session_id: str) -> dict:
        task_id = args.strip()
        if not task_id:
            return {'error': 'Usage: /reopen <task_id>'}

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        try:
            result = TaskOrchestrationService(self.db).reopen_failed_task(
                task_id=task_id,
                actor=f"chat:{session_id or 'anonymous'}",
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)

        self.db.refresh(task)
        return {
            'action': 'reopened',
            'task_id': task_id,
            'status': task.status,
            'gate_record_id': result.gate_record.id,
            'task': self._task_snapshot(task),
        }

    async def _handle_ask_human(self, args: str, session_id: str) -> dict:
        """CTV2-1400: the only channel from coordination out to a human.

        One-way. There is no get_answer / wait_for_human / poll here, and
        there never will be -- the human answers by typing in the chat
        session, a path that does not go through any tool (spec
        017d9cd4-b736-4dcb-8f8a-7880cb6f3a75). Calling this only queues the
        Telegram-side `human_question` event; it does not block.
        """
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid ask_human payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}

        question = str(payload.get('question') or '').strip()
        if not question:
            return {'error': 'question is required and must be non-empty'}

        why_human = str(payload.get('why_human') or '').strip()
        if not why_human:
            return {
                'error': (
                    "why_human is required and must be non-empty. This is not "
                    "machine escalation -- explain why only a human can answer "
                    "(e.g. an irreversible choice, a design tradeoff, missing "
                    "authority to decide). If a machine can decide this, don't "
                    "call ask_human."
                )
            }

        task_id = payload.get('task_id')
        task: Task | None = None
        if task_id:
            task_id = str(task_id).strip()
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return {'error': f'Task {task_id} not found'}

        options = payload.get('options')
        if options is not None and not (
            isinstance(options, list) and all(isinstance(o, str) for o in options)
        ):
            return {'error': 'options must be an array of strings'}

        from app.services.task_event_service import emit_task_event

        event = emit_task_event(
            task_id=task_id or None,
            event_type='human_question',
            payload={
                'question': question,
                'why_human': why_human,
                'options': options or [],
                'asked_by': f"chat:{session_id or 'anonymous'}",
            },
            db=self.db,
            kind='decision',
        )

        # Label the task as waiting on a HUMAN, not on a machine, so the
        # coordinator does not mistake the silence for a hang (spec
        # e6ee1eb0 / 017d9cd4). Reuses `awaiting_approval` +
        # `approval_prompt`, the same projection that already means
        # "workflow_state == waiting_human" for gates -- skip terminal
        # tasks, whose `awaiting_approval` is constrained to False.
        if task is not None and task.status not in ('done', 'cancelled'):
            task.awaiting_approval = True
            task.approval_prompt = f"[human_question] {question}"
            self.db.add(task)

        self.db.commit()
        return {
            'action': 'asked',
            'event_id': event.id,
            'task_id': task_id,
            'question': question,
            'note': 'One-way: wait for the human to answer in chat, do not poll for a response.',
        }

    async def _handle_verdict(self, args: str, session_id: str) -> dict:
        parts = args.strip().split(maxsplit=2)
        if len(parts) < 2:
            return {'error': 'Usage: /verdict <task_id> <pass|changes>'}

        task_id, verdict = parts[0], parts[1].lower()
        if verdict not in ['pass', 'changes']:
            return {'error': 'Verdict must be pass or changes'}

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        project = self.db.query(Project).filter(Project.id == task.project).first()
        if not project or not project.repo_root:
            return {'error': f'Project {task.project} has no repo_root'}
        review_path = Path(project.repo_root) / '.ct' / f'review-{task_id}.json'
        if not review_path.exists():
            return {'error': f'Review file not found: {review_path}'}
        try:
            with open(review_path) as f:
                review_data = json.load(f)
            ac_results = review_data.get('ac_results', [])
            findings = review_data.get('findings', [])
        except (json.JSONDecodeError, IOError) as exc:
            return {'error': f'Failed to load review file: {exc}'}
        # This manual/chat path has no AgentRun in hand (unlike the automated
        # worker path in cli_executor._submit_review_verdict), so the review
        # cycle is resolved from the task's CURRENT round -- never "most
        # recent for the task", which was the four-eyes-by-time hole this
        # table exists to close (CTV2-1379).
        review_cycle = (
            self.db.query(ReviewCycle)
            .filter(
                ReviewCycle.task_id == task_id,
                ReviewCycle.task_round_id == task.current_round_id,
            )
            .order_by(ReviewCycle.created_at.desc())
            .first()
        )
        if review_cycle is None:
            return {'error': f'No review_cycle found for task {task_id} current round'}
        try:
            result = TaskOrchestrationService(self.db).request_verdict(
                task_id=task_id,
                verdict=verdict,
                ac_results=ac_results,
                findings=findings,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(session_id, "verdict", args),
                review_cycle_id=review_cycle.id,
            )
        except OrchestrationError as exc:
            return self._orchestration_error_payload(exc, task_id)
        
        return {
            'action': 'verdict',
            'task_id': task_id,
            'verdict': verdict,
            'new_status': result.task.status,
            'decision_status': result.status,
            'gate_record_id': result.gate_record.id,
            'task': self._task_snapshot(result.task),
        }

    async def _handle_approve_gate(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import advance_task, run_agent

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /approve <gate_record_id> [approved|rejected]'}
        raw_id = parts[0]
        decision = parts[1].lower() if len(parts) > 1 else 'approved'

        if raw_id.startswith('admin:'):
            return await self._decide_admin_gate(raw_id, decision, session_id)

        try:
            gate_record_id = int(raw_id)
            gate_rec = self.db.query(GateRecord).filter(GateRecord.id == gate_record_id).first()
            if gate_rec is None:
                return {'error': f'Gate record {gate_record_id} not found'}
            task_id = gate_rec.task_id
        except ValueError:
            task_id = raw_id
            pending = (
                self.db.query(GateRecord)
                .filter(
                    GateRecord.task_id == task_id,
                    GateRecord.status == "pending",
                )
                .order_by(GateRecord.id.desc())
                .first()
            )
            if pending is None:
                return {'error': f'No pending gate found for task {task_id}'}
            gate_record_id = pending.id
            gate_rec = pending
        gate_payload = gate_rec.input_payload or {}
        service = TaskOrchestrationService(self.db)
        try:
            result = service.decide_gate(
                gate_record_id=gate_record_id,
                decision=decision,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(session_id, "approve", args, self._approve_attempt(task_id)),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        run = result.agent_run
        if run is not None:
            context = result.context or {}
            try:
                message = run_agent.send(
                    run.id,
                    run.task_id,
                    run.command,
                    context['repo_root'],
                    run.timeout_seconds,
                )
                run.dramatiq_message_id = str(message.message_id)
                self.db.commit()
            except Exception as exc:
                error = f'Could not queue run: {exc}'
                service.record_dispatch_queue_failure(
                    run_id=run.id,
                    error=error,
                    actor='system:dispatch-queue',
                    idempotency_key=(
                        f'{result.gate_record.idempotency_key}:queue-failure'
                    ),
                )
                self.db.refresh(result.task)
                return {'error': error, 'run_id': run.id, 'task': self._task_snapshot(result.task)}
        nudged: bool | None = None
        if result.applied:
            try:
                advance_task.send(result.task.id, "gate_approved")
            except Exception:
                nudged = False
                logger.warning(
                    "Failed to nudge orchestration driver after gate approval task=%s",
                    result.task.id,
                    exc_info=True,
                )
            else:
                nudged = True
        response = {
            'action': 'gate_decision',
            'task_id': result.task.id,
            'decision': result.status,
            'new_status': result.task.status,
            'run_id': run.id if run is not None else None,
            'nudged': nudged,
            'task': self._task_snapshot(result.task),
        }
        if gate_rec.gate_type == 'review_order':
            response.update({
                'reviewer': gate_payload.get('reviewer'),
                'selection_reason': gate_payload.get('selection_reason'),
            })
        return response

    async def _handle_update_task(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid update_task payload'}
        task_id = str(payload.get('task_id', '')).strip()
        patch = payload.get('patch')
        if not task_id or not isinstance(patch, Mapping) or not patch:
            return {'error': 'task_id and a non-empty patch object are required'}

        patch = dict(patch)
        add_deps = [str(d) for d in patch.pop('add_depends_on', None) or []]
        remove_deps = [str(d) for d in patch.pop('remove_depends_on', None) or []]
        actor = f"chat:{session_id or 'anonymous'}"
        service = TaskOrchestrationService(self.db)

        # coordinator_notes (CTV2-1397) is a coordinator-only column that the
        # planner reads but write_spec_plan never overwrites -- it does not
        # belong in state_machine.update_task_fields's generic patch
        # whitelist (that whitelist covers fields any actor at any status may
        # touch; coordinator_notes has its own, narrower rule: never touched
        # by the planner's write path). Handle it here instead so a plan run
        # in flight can't silently clobber it -- that was the original bug.
        has_coordinator_notes = 'coordinator_notes' in patch
        coordinator_notes_value = patch.pop('coordinator_notes', None)
        wrote_plan_field = 'plan' in patch

        try:
            for dep_id in add_deps:
                service.add_dependency(
                    task_id=task_id, depends_on_task_id=dep_id, actor=actor
                )
            if remove_deps:
                self.db.query(TaskDependency).filter(
                    TaskDependency.task_id == task_id,
                    TaskDependency.depends_on_task_id.in_(remove_deps),
                ).delete(synchronize_session=False)
                self.db.commit()
            task = (
                service.update_task_fields(task_id=task_id, patch=patch, actor=actor)
                if patch
                else self.db.get(Task, task_id)
            )
            if task is None:
                task = self.db.get(Task, task_id)
            if has_coordinator_notes and task is not None:
                task.coordinator_notes = coordinator_notes_value
                task.updated_at = datetime.now(timezone.utc)
                self.db.add(
                    AuditLog(
                        task_id=task.id,
                        action='update_task_coordinator_notes',
                        actor=actor,
                        details={'coordinator_notes': coordinator_notes_value},
                    )
                )
                self.db.commit()
                self.db.refresh(task)
        except OrchestrationError as exc:
            return {'error': str(exc)}
        if task is None:
            return {'error': f"Task '{task_id}' not found"}

        depends_on = [
            dep.depends_on_task_id
            for dep in self.db.query(TaskDependency)
            .filter(TaskDependency.task_id == task_id)
            .all()
        ]
        response: dict[str, Any] = {
            'action': 'updated',
            'task_id': task.id,
            'plan': task.plan,
            'coordinator_notes': task.coordinator_notes,
            'acceptance_criteria': task.acceptance_criteria,
            'priority': task.priority,
            'tags': task.tags,
            'raw_input': task.raw_input,
            'depends_on': depends_on,
        }

        warnings: list[str] = []
        if wrote_plan_field:
            warnings.append(
                "'plan' is planner output -- write_spec_plan overwrites it on the "
                "next run. If this was a reply/decision for the planner to read, "
                "put it in 'coordinator_notes' instead."
            )
        if has_coordinator_notes:
            active_run = find_active_plan_run(self.db, task_id)
            if active_run is not None:
                started = active_run.started_at or active_run.queued_at
                warnings.append(
                    f"plan run {active_run.id} đang chạy, nó khởi động lúc {started} "
                    "nên sẽ KHÔNG đọc thay đổi này. Chờ run xong rồi chạy lại "
                    "generate_spec_plan."
                )
        if warnings:
            response['warnings'] = warnings
        return response

    async def _handle_get_status(self, args: str, session_id: str) -> dict:
        if not self.db:
            return {'error': 'Database session not available'}
        
        target_id = args.strip() if args else None
        
        if target_id:
            task = self.db.query(Task).filter(Task.id == target_id).first()
            if not task:
                task = self.db.query(Task).filter(Task.id.ilike(f"%{target_id}%")).first()
            if task:
                pending = self._pending_gate(task.id)
                return {
                    'status': 'success',
                    'task': {
                        'id': task.id,
                        'title': task.title,
                        'project': task.project,
                        'status': task.status,
                        'current_gate': task.current_gate,
                        'executor': task.executor,
                        'reviewer': task.reviewer,
                        'planner': task.planner,
                        'plan_critic': task.plan_critic,
                        'plan_critic_status': task.plan_critic_status,
                        'mode': task.mode,
                        'result_ref': task.result_ref,
                        'landed_ref': task.landed_ref,
                        'awaiting_approval': bool(pending) or bool(task.awaiting_approval),
                        'approval_prompt': task.approval_prompt,
                        'pending_gate': {
                            'gate_record_id': pending.id,
                            'gate_type': pending.gate_type,
                            'created_at': pending.created_at.isoformat() if pending.created_at else None,
                        } if pending else None,
                        'error': task.error,
                        'next_step': TaskValidator(self.db).describe_next_step(task),
                        'available_actions': TaskValidator(self.db).available_actions(task),
                        **self._open_questions_status(task),
                    }
                }
            return {'error': f"Task '{target_id}' not found"}
        
        if session_id:
            task = self.db.query(Task).filter(Task.session_id == session_id).first()
            if not task:
                db_session = self.db.query(SessionModel).filter(
                    (SessionModel.id == session_id) | (SessionModel.thread_id == session_id)
                ).first()
                if db_session and db_session.task_id:
                    task = self.db.query(Task).filter(Task.id == db_session.task_id).first()
            
            if task:
                pending = self._pending_gate(task.id)
                return {
                    'status': 'success',
                    'task': {
                        'id': task.id,
                        'title': task.title,
                        'project': task.project,
                        'status': task.status,
                        'current_gate': task.current_gate,
                        'executor': task.executor,
                        'reviewer': task.reviewer,
                        'planner': task.planner,
                        'plan_critic': task.plan_critic,
                        'plan_critic_status': task.plan_critic_status,
                        'mode': task.mode,
                        'result_ref': task.result_ref,
                        'landed_ref': task.landed_ref,
                        'awaiting_approval': bool(pending) or bool(task.awaiting_approval),
                        'approval_prompt': task.approval_prompt,
                        'pending_gate': {
                            'gate_record_id': pending.id,
                            'gate_type': pending.gate_type,
                            'created_at': pending.created_at.isoformat() if pending.created_at else None,
                        } if pending else None,
                        'error': task.error,
                        'next_step': TaskValidator(self.db).describe_next_step(task),
                        'available_actions': TaskValidator(self.db).available_actions(task),
                        **self._open_questions_status(task),
                    }
                }
        
        tasks = self.db.query(Task).order_by(Task.updated_at.desc()).limit(10).all()
        return {
            'status': 'success',
            'tasks': [
                {
                    'id': t.id,
                    'title': t.title,
                    'project': t.project,
                    'status': t.status,
                    'current_gate': t.current_gate,
                    'executor': t.executor,
                    'reviewer': t.reviewer,
                }
                for t in tasks
            ]
        }
