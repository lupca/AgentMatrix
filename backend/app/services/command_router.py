import re
import hashlib
import json
from typing import Tuple, Optional
from app.db.models import AgentRun, Task, Session as SessionModel
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
)

COMMANDS = {
    '/pm': 'create_task',
    '/dispatch': 'dispatch_task',
    '/verdict': 'verdict',
    '/approve': 'approve_gate',
    '/status': 'get_status',
    '/cancel': 'cancel_task',
    '/compact': 'compact_context',
    '/help': 'show_help',
}

class CommandRouter:
    def __init__(self, db_session):
        self.db = db_session
    
    def parse(self, message: str) -> Tuple[Optional[str], str]:
        message = message.strip()
        if not message.startswith('/'):
            return None, message
        parts = message.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ''
        if cmd in COMMANDS:
            return COMMANDS[cmd], args
        return None, message
    
    async def execute(self, command: str, args: str, session_id: str) -> dict:
        handler = getattr(self, f'_handle_{command}', None)
        if not handler:
            return {'error': f'Unknown command: {command}'}
        return await handler(args, session_id)
    
    async def _handle_show_help(self, args: str, session_id: str) -> dict:
        return {'commands': list(COMMANDS.keys())}

    async def _handle_create_task(self, args: str, session_id: str) -> dict:
        import uuid
        from datetime import datetime
        
        # Parse args: 'task title --project name'
        project = 'default'
        title = args
        if '--project' in args:
            parts = args.split('--project')
            title = parts[0].strip()
            project = parts[1].strip().split()[0] if parts[1].strip() else 'default'
        
        # Generate task ID
        prefix = project.upper().replace('-', '')[:4]
        count = self.db.query(Task).filter(Task.project == project).count() + 1
        task_id = f'{prefix}-{count:03d}'
        
        task = Task(
            id=task_id,
            title=title,
            project=project,
            status='todo',
            current_gate='spec',
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self.db.add(task)
        self.db.commit()
        
        return {'action': 'created', 'task_id': task.id, 'title': title, 'project': project}

    async def _handle_dispatch_task(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import run_agent
        from app.db.models import Agent
        from app.services.agent_matcher import AgentMatcher

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /dispatch <task_id> [agent_id]'}

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
                    session_id, "dispatch", args
                ),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        if not result.applied:
            return {
                'action': 'dispatch_pending',
                'task_id': task_id,
                'gate_record_id': result.gate_record.id,
                'status': 'pending',
            }

        run = result.agent_run
        context = result.context or {}
        if run is None:
            return {'error': 'Dispatch transition did not create an agent run'}
        try:
            run_agent.send(
                run.id,
                task_id,
                run.command,
                context['repo_root'],
                run.timeout_seconds,
            )
        except Exception as exc:
            error = f'Could not queue run: {exc}'
            service.record_dispatch_queue_failure(
                run_id=run.id,
                error=error,
                actor='system:dispatch-queue',
                idempotency_key=f'{result.gate_record.idempotency_key}:queue-failure',
            )
            return {'error': error, 'run_id': run.id}
        
        return {'action': 'dispatched', 'task_id': task_id, 'run_id': run.id, 'agent': agent_id}

    async def _handle_cancel_task(self, args: str, session_id: str) -> dict:
        from datetime import datetime, timezone
        from app.db.models import AgentRun
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
        return {'action': 'cancelled', 'task_id': task_id, 'run_id': run.id, 'status': 'cancelled'}

    async def _handle_verdict(self, args: str, session_id: str) -> dict:
        parts = args.strip().split(maxsplit=2)
        if len(parts) < 3:
            return {
                'error': (
                    'Usage: /verdict <task_id> <pass|changes> '
                    '<ac_results_json>'
                )
            }
        
        task_id, verdict = parts[0], parts[1].lower()
        if verdict not in ['pass', 'changes']:
            return {'error': 'Verdict must be pass or changes'}
        
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}
        
        try:
            ac_results = json.loads(parts[2])
        except json.JSONDecodeError:
            return {'error': 'ac_results_json must be valid JSON'}
        try:
            result = TaskOrchestrationService(self.db).request_verdict(
                task_id=task_id,
                verdict=verdict,
                ac_results=ac_results,
                actor=task.reviewer or f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(session_id, "verdict", args),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}
        
        return {
            'action': 'verdict',
            'task_id': task_id,
            'verdict': verdict,
            'new_status': result.task.status,
            'decision_status': result.status,
            'gate_record_id': result.gate_record.id,
        }

    async def _handle_approve_gate(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import run_agent

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /approve <gate_record_id> [approved|rejected]'}
        try:
            gate_record_id = int(parts[0])
        except ValueError:
            return {'error': 'gate_record_id must be an integer'}
        decision = parts[1].lower() if len(parts) > 1 else 'approved'
        service = TaskOrchestrationService(self.db)
        try:
            result = service.decide_gate(
                gate_record_id=gate_record_id,
                decision=decision,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(session_id, "approve", args),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        run = result.agent_run
        if run is not None:
            context = result.context or {}
            try:
                run_agent.send(
                    run.id,
                    run.task_id,
                    run.command,
                    context['repo_root'],
                    run.timeout_seconds,
                )
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
                return {'error': error, 'run_id': run.id}
        return {
            'action': 'gate_decision',
            'task_id': result.task.id,
            'decision': result.status,
            'new_status': result.task.status,
            'run_id': run.id if run is not None else None,
        }

    @staticmethod
    def _command_key(session_id: str, action: str, args: str) -> str:
        digest = hashlib.sha256(args.strip().encode("utf-8")).hexdigest()[:24]
        return f"chat:{session_id or 'anonymous'}:{action}:{digest}"[:100]

    async def _handle_get_status(self, args: str, session_id: str) -> dict:
        if not self.db:
            return {'error': 'Database session not available'}
        
        target_id = args.strip() if args else None
        
        if target_id:
            task = self.db.query(Task).filter(Task.id == target_id).first()
            if not task:
                task = self.db.query(Task).filter(Task.id.ilike(f"%{target_id}%")).first()
            if task:
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
                        'mode': task.mode,
                        'awaiting_approval': task.awaiting_approval,
                        'error': task.error,
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
                        'mode': task.mode,
                        'awaiting_approval': task.awaiting_approval,
                        'error': task.error,
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

    async def _handle_compact_context(self, args: str, session_id: str) -> dict:
        from app.services.context_hierarchy import ContextHierarchy
        session = self.db.query(SessionModel).filter(
            (SessionModel.id == session_id) | (SessionModel.thread_id == session_id)
        ).first()
        if not session:
            return {'error': f'Session {session_id} not found'}

        ctx = ContextHierarchy(self.db)
        compacted = ctx.compact_context(session, threshold=0)
        return {'action': 'compacted', 'session_id': session_id, 'compacted': compacted}
