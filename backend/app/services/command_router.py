import asyncio
import re
import hashlib
import json
import os
import logging
from collections.abc import Mapping
from typing import Any, Tuple, Optional
from app.db.models import (
    Agent,
    AgentRun,
    AgentOutputChunk,
    AuditLog,
    GateRecord,
    KnowledgeItem,
    LLMUsage,
    RunResourceUsage,
    Project,
    Setting,
    Task,
    TaskDependency,
    TaskEvent,
    Session as SessionModel,
)
from app.services import entity_admin
from app.services.admin_gate import AdminGateService, AdminOrchestrationError
from app.services.agent_suggester import AgentSuggester
from app.services.archive import ArchiveError, ArchiveService
from app.services.crypto import encrypt_api_key
from app.db.archive import with_archived
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
)

logger = logging.getLogger(__name__)
from app.services.tool_registry import (
    DEFERRED_GROUPS,
    TOOL_REGISTRY,
    dump_registry,
    get_group_tool_definitions,
    resolve_tool_name,
)
from app.services.graph_client import (
    get_impact_radius as graph_get_impact_radius,
    semantic_search,
)
from app.services.llm_service import ConfigurationError

# query_db (ADR-001 §D2): entity + filter-field whitelist, and a compact
# serializer per entity so responses stay small and never leak secrets
# (notably Agent.api_key, which is deliberately absent below).
_QUERY_DB_ENTITIES: dict[str, dict[str, Any]] = {
    "tasks": {
        "model": Task,
        "filters": {
            "id": "str",
            "status": "str",
            "project": "str",
            "executor": "str",
            "reviewer": "str",
            "priority": "str",
            "risk": "str",
            "mode": "str",
        },
        "order_by": Task.updated_at.desc(),
        "serialize": lambda t: {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "project": t.project,
            "executor": t.executor,
            "reviewer": t.reviewer,
            "current_gate": t.current_gate,
        },
    },
    "projects": {
        "model": Project,
        "filters": {"id": "str", "status": "str"},
        "order_by": Project.id.asc(),
        "serialize": lambda p: {
            "id": p.id,
            "name": p.name,
            "status": p.status,
        },
    },
    "agents": {
        "model": Agent,
        "filters": {
            "id": "str",
            "role": "str",
            "status": "str",
            "agent_type": "str",
            "cli": "str",
            "is_default": "bool",
        },
        "order_by": Agent.id.asc(),
        "serialize": lambda a: {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "status": a.status,
            "agent_type": a.agent_type,
            "model": a.model,
            "is_default": a.is_default,
        },
    },
    "sessions": {
        "model": SessionModel,
        "filters": {
            "id": "str",
            "status": "str",
            "context_level": "str",
            "project_id": "str",
            "task_id": "str",
            "pinned": "bool",
        },
        "order_by": SessionModel.last_activity_at.desc(),
        "serialize": lambda s: {
            "id": s.id,
            "title": s.title,
            "status": s.status,
            "context_level": s.context_level,
            "project_id": s.project_id,
            "task_id": s.task_id,
        },
    },
    "knowledge": {
        "model": KnowledgeItem,
        "filters": {"id": "str", "category": "str", "project": "str", "author": "str"},
        "order_by": KnowledgeItem.updated_at.desc(),
        "serialize": lambda k: {
            "id": k.id,
            "title": k.title,
            "category": k.category,
            "project": k.project,
        },
    },
    "usage": {
        "model": LLMUsage,
        "filters": {
            "id": "str",
            "session_id": "str",
            "task_id": "str",
            "operation": "str",
            "model": "str",
            "provider": "str",
        },
        "order_by": LLMUsage.created_at.desc(),
        "serialize": lambda u: {
            "id": u.id,
            "model": u.model,
            "operation": u.operation,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": float(u.cost_usd) if u.cost_usd is not None else 0.0,
        },
    },
    "settings": {
        "model": Setting,
        "filters": {"id": "str"},
        "order_by": Setting.key.asc(),
        "serialize": lambda s: {
            "key": s.key,
            "value": s.value,
            "description": s.description,
        },
    },
    "agent_runs": {
        "model": AgentRun,
        "filters": {
            "id": "str",
            "task_id": "str",
            "agent_id": "str",
            "status": "str",
            "kind": "str",
        },
        "order_by": AgentRun.queued_at.desc(),
        "serialize": lambda r: {
            "id": r.id,
            "task_id": r.task_id,
            "agent_id": r.agent_id,
            "kind": r.kind,
            "status": r.status,
            "effort": r.effort,
            "exit_code": r.exit_code,
            "queued_at": r.queued_at.isoformat() if r.queued_at else None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        },
    },
    "audit": {
        "model": AuditLog,
        "filters": {"task_id": "str", "action": "str", "actor": "str"},
        "order_by": AuditLog.created_at.desc(),
        "serialize": lambda a: {
            "id": a.id,
            "task_id": a.task_id,
            "action": a.action,
            "actor": a.actor,
            "details": a.details,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        },
    },
}


def _coerce_filter_value(raw: str, kind: str) -> Any:
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes")
    return raw

# Derived from the tool registry (single source of truth, ADR-001 §D1) by
# slash_alias, plus '/help' which is a router-only command, not a tool.
COMMANDS = {
    spec.slash_alias: spec.handler
    for spec in TOOL_REGISTRY.values()
    if spec.slash_alias
}
COMMANDS['/help'] = 'show_help'

# '/help' is router-only (not a TOOL_REGISTRY entry) so it's projected here in
# the same shape as dump_registry() entries for a single UI/help data source.
HELP_COMMAND = {
    'name': 'help',
    'description': 'List available commands and tools.',
    'slash_alias': '/help',
    'tier': 'eager',
    'group': 'meta',
}

class CommandRouter:
    def __init__(self, db_session):
        self.db = db_session

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
        }

    def _pending_gate(self, task_id: str) -> GateRecord | None:
        """Return the newest pending gate that has not been decided."""

        decided_parent_ids = (
            self.db.query(GateRecord.parent_id)
            .filter(GateRecord.parent_id.isnot(None))
        )
        return (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.status == 'pending',
                GateRecord.id.notin_(decided_parent_ids),
            )
            .order_by(GateRecord.id.desc())
            .first()
        )
    
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

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        session_id: str,
    ) -> dict[str, Any]:
        """Execute a model tool using the existing slash-command handlers.

        The LLM-facing schemas use structured JSON while the original chat
        router uses command strings. Keeping this translation here gives both
        entry points the same gate and persistence behavior.
        """

        if not isinstance(arguments, Mapping):
            return {'error': 'Tool arguments must be a JSON object'}

        canonical_name = resolve_tool_name(tool_name)
        spec = TOOL_REGISTRY.get(canonical_name)
        if spec is None:
            return {'error': f'Unknown tool: {tool_name}'}

        args = dict(arguments)
        if canonical_name == 'create_task':
            title = str(args.get('title', '')).strip()
            if not title:
                return {'error': 'title is required'}
            project = str(args.get('project', '')).strip()
            depends_on = args.get('depends_on') or []
            command_args = title + (f' --project {project}' if project else '')
            if depends_on:
                command_args += ' --depends-on ' + ','.join(str(d) for d in depends_on)
        elif canonical_name == 'get_status':
            command_args = str(args.get('task_id', '') or '')
        elif canonical_name == 'get_run_output':
            task_id = str(args.get('task_id', '')).strip()
            run_id = str(args.get('run_id', '')).strip()
            if not task_id or not run_id:
                return {'error': 'task_id and run_id are required'}
            command_args = json.dumps({
                'task_id': task_id,
                'run_id': run_id,
                'offset': args.get('offset', 0),
                'limit': args.get('limit', 20),
            }, ensure_ascii=False)
        elif canonical_name == 'get_stats':
            command_args = json.dumps({
                'task_id': args.get('task_id'),
                'agent_id': args.get('agent_id'),
            }, ensure_ascii=False)
        elif canonical_name == 'get_task_events':
            command_args = json.dumps({
                'task_id': args.get('task_id'),
                'since_id': args.get('since_id'),
                'kind': args.get('kind'),
                'event_types': args.get('event_types'),
                'limit': args.get('limit', 50),
            }, ensure_ascii=False)
        elif canonical_name == 'wait_for_task':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = json.dumps({
                'task_id': task_id,
                'since_event_id': args.get('since_event_id'),
                'timeout_seconds': args.get('timeout_seconds', 55),
            }, ensure_ascii=False)
        elif canonical_name == 'archive_task':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = json.dumps({
                'task_id': task_id,
                'restore': bool(args.get('restore', False)),
            }, ensure_ascii=False)
        elif canonical_name == 'suggest_agents':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = json.dumps({
                'task_id': task_id,
                'role': args.get('role', 'executor'),
                'top_n': args.get('top_n', 3),
            }, ensure_ascii=False)
        elif canonical_name == 'query_db':
            if 'sql' in args:
                command_args = json.dumps({'sql': args['sql']})
            else:
                entity = str(args.get('entity', '')).strip()
                if not entity:
                    return {'error': 'entity or sql is required'}
                filters = args.get('filters') or {}
                if not isinstance(filters, Mapping):
                    return {'error': 'filters must be a JSON object'}
                limit = args.get('limit', 20)
                offset = args.get('offset', 0)
                tokens = [entity]
                tokens.extend(f'{key}={value}' for key, value in filters.items())
                tokens.append(f'limit={limit}')
                tokens.append(f'offset={offset}')
                command_args = ' '.join(str(token) for token in tokens)
        elif canonical_name == 'dispatch_task':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            # Accept agent_id as an alias: generate_spec_plan uses agent_id,
            # so callers naturally use it here too — dropping it silently
            # handed the choice to the matcher (CTV2-228).
            executor = str(args.get('executor') or args.get('agent_id') or '').strip()
            effort = str(args.get('effort', '') or '').strip()
            command_args = ' '.join(
                part for part in (task_id, executor, f'--effort {effort}' if effort else '') if part
            )
        elif canonical_name == 'record_verdict':
            task_id = str(args.get('task_id', '')).strip()
            verdict = str(args.get('verdict', '')).strip().lower()
            if not task_id or not verdict:
                return {'error': 'task_id and verdict are required'}
            findings = args.get('findings', [])
            command_args = f'{task_id} {verdict} {json.dumps(findings, ensure_ascii=False)}'
        elif canonical_name == 'request_review':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            reviewer = str(args.get('reviewer') or args.get('agent_id') or '').strip()
            command_args = ' '.join(part for part in (task_id, reviewer) if part)
        elif canonical_name == 'generate_spec_plan':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            agent_id = str(args.get('agent_id', '') or '').strip()
            command_args = ' '.join(part for part in (task_id, agent_id) if part)
        elif canonical_name == 'approve_gate':
            gate_id = args.get('gate_record_id', args.get('task_id'))
            if gate_id is None:
                return {'error': 'gate_record_id is required'}
            # The decision must survive the JSON->string mapping: dropping it
            # silently turned every human veto into an approval (CTV2-233).
            decision = str(args.get('decision', 'approved') or 'approved').strip().lower()
            if decision in {'approve', 'approved', 'yes', 'y'}:
                decision = 'approved'
            elif decision in {'reject', 'rejected', 'no', 'n', 'deny', 'denied'}:
                decision = 'rejected'
            else:
                return {'error': f"Unsupported decision {decision!r}: use 'approved' or 'rejected'"}
            command_args = f'{gate_id} {decision}'
        elif canonical_name == 'cancel_task':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = task_id
        elif canonical_name == 'land_task':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = task_id
        elif canonical_name == 'compact_context':
            command_args = ''
        elif canonical_name == 'manage_project':
            action = str(args.get('action', '')).strip()
            if not action:
                return {'error': 'action is required'}
            command_args = json.dumps(args, ensure_ascii=False)
        elif canonical_name == 'manage_agent':
            action = str(args.get('action', '')).strip()
            if not action:
                return {'error': 'action is required'}
            command_args = json.dumps(args, ensure_ascii=False)
        elif canonical_name == 'manage_knowledge':
            action = str(args.get('action', '')).strip()
            if not action:
                return {'error': 'action is required'}
            command_args = json.dumps(args, ensure_ascii=False)
        elif canonical_name == 'update_settings':
            key = str(args.get('key', '')).strip()
            if not key:
                return {'error': 'key is required'}
            if 'value' not in args:
                return {'error': 'value is required'}
            command_args = json.dumps(args, ensure_ascii=False)
        elif canonical_name == 'update_task':
            task_id = str(args.get('task_id', '')).strip()
            patch = args.get('patch')
            if not task_id or not isinstance(patch, Mapping):
                return {'error': 'task_id and patch are required'}
            command_args = json.dumps(
                {'task_id': task_id, 'patch': dict(patch)}, ensure_ascii=False
            )
        elif canonical_name == 'load_tools':
            group = str(args.get('group', '')).strip()
            if not group:
                return {'error': 'group is required'}
            command_args = group
        elif canonical_name == 'get_minimal_context':
            query = str(args.get('query', '')).strip()
            if not query:
                return {'error': 'query is required'}
            command_args = json.dumps({
                'query': query,
                'limit': args.get('limit', 10),
            }, ensure_ascii=False)
        elif canonical_name == 'get_impact_radius':
            file = str(args.get('file', '')).strip()
            if not file:
                return {'error': 'file is required'}
            command_args = file
        elif canonical_name == 'save_project_context':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            project_id = str(args.get('project_id', '')).strip()
            if not project_id:
                return {'error': 'project_id is required'}
            if 'context_md' not in args:
                return {'error': 'context_md is required'}
            command_args = json.dumps({
                'task_id': task_id,
                'project_id': project_id,
                'context_md': args.get('context_md'),
                'rules': args.get('rules') or [],
            }, ensure_ascii=False)
        else:
            return {'error': f'Unknown tool: {tool_name}'}

        return await self.execute(spec.handler, command_args, session_id)
    
    async def _handle_show_help(self, args: str, session_id: str) -> dict:
        return {'commands': dump_registry() + [HELP_COMMAND]}

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

    async def _handle_get_stats(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid get_stats arguments'}
        task_id = str(payload.get('task_id') or '').strip() or None
        agent_id = str(payload.get('agent_id') or '').strip() or None
        usage_query = self.db.query(LLMUsage)
        if task_id:
            usage_query = usage_query.filter(LLMUsage.task_id == task_id)
        if agent_id:
            usage_query = usage_query.join(AgentRun, LLMUsage.agent_run_id == AgentRun.id).filter(
                AgentRun.agent_id == agent_id
            )
        usage = usage_query.all()
        resources = self.db.query(RunResourceUsage)
        if task_id or agent_id:
            resources = resources.join(AgentRun, RunResourceUsage.agent_run_id == AgentRun.id)
            if task_id:
                resources = resources.filter(AgentRun.task_id == task_id)
            if agent_id:
                resources = resources.filter(AgentRun.agent_id == agent_id)
        return {
            'task_id': task_id,
            'agent_id': agent_id,
            'calls': len(usage),
            'input_tokens': sum(row.input_tokens or 0 for row in usage),
            'output_tokens': sum(row.output_tokens or 0 for row in usage),
            'cached_tokens': sum(row.cached_tokens or 0 for row in usage),
            'cost_usd': round(sum(float(row.cost_usd or 0) for row in usage), 8),
            'runs': resources.count(),
            'run_cost_usd': round(sum(float(row.estimated_cost_usd or 0) for row in resources), 8),
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
            # Resend this as since_id on the next call to only get new events.
            'cursor': events[-1].id if events else since_id,
            'has_more': len(events) == limit,
        }

    async def _handle_wait_for_task(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
            since_event_id = int(payload.get('since_event_id') or 0)
            timeout_seconds = min(120, max(5, int(payload.get('timeout_seconds') or 55)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid wait_for_task arguments'}
        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}

        _TERMINAL = {'done', 'failed', 'cancelled', 'changes-requested'}
        _POLL_INTERVAL = 2.0

        def _snapshot() -> tuple[dict | None, list[dict], int]:
            # End the previous transaction so READ COMMITTED shows fresh rows.
            self.db.rollback()
            task = self.db.get(Task, task_id)
            if task is None:
                return None, [], since_event_id
            events = (
                self.db.query(TaskEvent)
                .filter(TaskEvent.task_id == task_id, TaskEvent.id > since_event_id)
                .order_by(TaskEvent.id.asc())
                .limit(20)
                .all()
            )
            cursor = events[-1].id if events else since_event_id
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

        task_dict, _, _ = _snapshot()
        if task_dict is None:
            return {'error': f"Task '{task_id}' not found"}
        initial_status = task_dict['status']

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
                    # Resend this as since_event_id on the next wait_for_task call.
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

    async def _handle_suggest_agents(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
            top_n = min(10, max(1, int(payload.get('top_n') or 3)))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {'error': 'Invalid suggest_agents arguments'}
        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}
        role = str(payload.get('role') or 'executor').strip()
        task = self.db.get(Task, task_id)
        if task is None:
            return {'error': f"Task '{task_id}' not found"}
        try:
            suggestions = AgentSuggester(self.db).suggest(task, role=role, top_n=top_n)
        except ValueError as exc:
            return {'error': str(exc)}
        return {
            'task_id': task_id,
            'role': role,
            'suggestions': [
                {'agent_id': s.agent_id, 'score': s.score, 'reason': s.reason}
                for s in suggestions
            ],
        }

    async def _handle_load_tools(self, args: str, session_id: str) -> dict:
        group = args.strip()
        definitions = get_group_tool_definitions(group)
        if definitions is None:
            return {
                'error': (
                    f"Unknown tool group '{group}'. Valid groups: "
                    f"{', '.join(DEFERRED_GROUPS)}"
                )
            }
        return {
            'status': 'success',
            'group': group,
            'loaded': [tool['name'] for tool in definitions],
        }

    def _research_repo_root(self, session_id: str) -> tuple[str | None, dict | None]:
        """Resolve the project repository from persisted session scope."""
        session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
        project_id = session.project_id if session else None
        if session and session.task_id:
            task = self.db.query(Task).filter(Task.id == session.task_id).first()
            project_id = task.project if task else project_id
        if not project_id:
            return None, {'status': 'error', 'reason': 'research_requires_project_scope',
                          'suggestion': 'Use a project- or task-scoped session.'}
        project = self.db.query(Project).filter(Project.id == project_id).first()
        if not project or not project.repo_root:
            return None, {'status': 'error', 'reason': 'project_repo_root_not_configured',
                          'project_id': project_id,
                          'suggestion': 'Configure Project.repo_root before using research tools.'}
        return os.path.abspath(project.repo_root), None

    @staticmethod
    def _research_error(exc: Exception) -> dict:
        return {
            'status': 'error',
            'reason': 'graph_unavailable',
            'detail': str(exc),
            'suggestion': 'Build or refresh the code graph, then retry the research tool.',
        }

    async def _handle_get_minimal_context(self, args: str, session_id: str) -> dict:
        repo_root, error = self._research_repo_root(session_id)
        if error:
            return error
        try:
            payload = json.loads(args)
            result = await semantic_search(
                repo_root, str(payload['query']), int(payload.get('limit', 10)),
                raise_on_error=True, compress_output=True,
            )
        except Exception as exc:
            return self._research_error(exc)
        return {'status': 'success', 'repo_root': repo_root, 'context': result}

    async def _handle_get_impact_radius(self, args: str, session_id: str) -> dict:
        repo_root, error = self._research_repo_root(session_id)
        if error:
            return error
        try:
            result = await graph_get_impact_radius(
                repo_root, args.strip(), raise_on_error=True, compress_output=True,
            )
        except Exception as exc:
            return self._research_error(exc)
        return {'status': 'success', 'repo_root': repo_root, 'files': result}

    async def _handle_save_project_context(self, args: str, session_id: str) -> dict:
        import uuid
        from app.db.models import ProjectRule

        try:
            payload = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return {'error': 'Invalid arguments for save_project_context'}

        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}

        project_id = str(payload.get('project_id', '')).strip()
        if not project_id:
            return {'error': 'project_id is required'}

        context_md = payload.get('context_md')
        if not isinstance(context_md, str) or not context_md.strip():
            return {'error': 'context_md is required'}

        rules = payload.get('rules') or []
        if not isinstance(rules, list):
            return {'error': 'rules must be a list'}

        project = self.db.get(Project, project_id)
        if project is None:
            return {'error': f'Project {project_id} not found'}

        # The executor token is scoped to task_id (checked upstream by
        # _task_scope_ok), but project_id arrives straight from the client:
        # without this check an executor could overwrite ANY project's
        # context (round-3 review finding F1, cross-project write).
        task = self.db.get(Task, task_id)
        if task is None:
            return {'error': f'Task {task_id} not found'}
        if task.project != project_id:
            return {
                'error': (
                    f'Task {task_id} belongs to project {task.project}, '
                    f'not {project_id}; refusing cross-project context write'
                )
            }

        context_lines = context_md.splitlines()
        if len(context_lines) > 150:
            return {
                'error': (
                    f'context_md must be at most 150 lines, got {len(context_lines)}'
                )
            }

        if len(rules) > 5:
            return {'error': f'rules must contain at most 5 entries, got {len(rules)}'}

        parsed_rules: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for idx, rule in enumerate(rules):
            if not isinstance(rule, Mapping):
                return {'error': f'rule at index {idx} must be an object'}
            name = str(rule.get('name', '')).strip()
            content = rule.get('content')
            if not name:
                return {'error': f'rule at index {idx} is missing a name'}
            if len(name) > 100:
                return {'error': f"rule name '{name[:40]}...' exceeds 100 characters"}
            if name in seen_names:
                return {'error': f"duplicate rule name '{name}'; rule names must be unique"}
            seen_names.add(name)
            if not isinstance(content, str) or not content.strip():
                return {'error': f"rule '{name}' is missing content"}
            globs = rule.get('globs') or []
            if not isinstance(globs, list) or not all(
                isinstance(glob, str) for glob in globs
            ):
                return {'error': f"rule '{name}' globs must be a list of strings"}
            parsed_rules.append({
                'name': name,
                'globs': list(globs),
                'content': content[:3000],
            })

        project.context_md = context_md
        project.context_generated = True

        self.db.query(ProjectRule).filter(ProjectRule.project_id == project_id).delete()
        for rule in parsed_rules:
            self.db.add(ProjectRule(
                id=f'rule-{uuid.uuid4().hex[:12]}',
                project_id=project_id,
                name=rule['name'],
                globs=rule['globs'],
                content=rule['content'],
            ))

        self.db.commit()

        return {
            'status': 'success',
            'task_id': task_id,
            'project_id': project_id,
            'context_lines': len(context_lines),
            'rules_count': len(parsed_rules),
        }

    async def _handle_query_db(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args.strip().startswith('{') else None
        except json.JSONDecodeError:
            payload = None

        if payload and 'sql' in payload:
            from app.services.sql_guard import validate_select, SQLGuardError
            from app.db.base import get_readonly_db
            from sqlalchemy import text
            
            sql = payload['sql']
            
            session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            task_id = session.task_id if session else None
            
            def log_audit(status, details):
                audit = AuditLog(
                    task_id=task_id,
                    action="query_db_sql",
                    actor=f"chat:{session_id or 'anonymous'}",
                    details={"sql": sql, "status": status, **details}
                )
                self.db.add(audit)
                self.db.commit()

            try:
                guarded_sql = validate_select(sql)
            except SQLGuardError as e:
                log_audit("rejected", {"reason": str(e)})
                return {'error': str(e), 'hint': "Chỉ chấp nhận một câu SELECT duy nhất. Xem describe schema trong tool description."}
            
            try:
                readonly_db_gen = get_readonly_db()
                readonly_db = next(readonly_db_gen)
            except RuntimeError as e:
                log_audit("error", {"reason": str(e)})
                return {'error': str(e)}
            
            try:
                readonly_db.execute(text("SET TRANSACTION READ ONLY"))
                readonly_db.execute(text("SET LOCAL statement_timeout = '10s'"))
                result = readonly_db.execute(text(guarded_sql))
                rows = [dict(row._mapping) for row in result]
                truncated = len(rows) > 500
                if truncated:
                    rows = rows[:500]
                
                log_audit("success", {"truncated": truncated, "row_count": len(rows)})
                
                resp = {
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                }
                if truncated:
                    resp["hint"] = "Kết quả bị cắt ở 500 dòng — thêm WHERE, hoặc dùng COUNT(*)/GROUP BY để tổng hợp thay vì lật trang."
                return resp
            except Exception as e:
                log_audit("error", {"reason": str(e)})
                return {'error': f"Database error: {str(e)}"}
            finally:
                readonly_db.close()

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /query <entity> [field=value ...] [limit=N] [offset=N]'}

        entity = parts[0].lower()
        entity_spec = _QUERY_DB_ENTITIES.get(entity)
        if entity_spec is None:
            return {
                'error': (
                    f"Unknown entity '{entity}'. Valid entities: "
                    f"{', '.join(sorted(_QUERY_DB_ENTITIES))}"
                )
            }

        filters: dict[str, Any] = {}
        limit = 20
        offset = 0
        include_archived = False
        for token in parts[1:]:
            if '=' not in token:
                return {'error': f"Invalid filter token '{token}', expected field=value"}
            key, _, value = token.partition('=')
            if key == 'limit':
                try:
                    limit = int(value)
                except ValueError:
                    return {'error': 'limit must be an integer'}
            elif key == 'offset':
                try:
                    offset = int(value)
                except ValueError:
                    return {'error': 'offset must be an integer'}
            elif key == 'include_archived':
                include_archived = _coerce_filter_value(value, 'bool')
            elif key in entity_spec['filters']:
                filters[key] = _coerce_filter_value(value, entity_spec['filters'][key])
            else:
                return {
                    'error': (
                        f"Unknown filter '{key}' for entity '{entity}'. "
                        f"Allowed filters: {', '.join(sorted(entity_spec['filters']))}"
                    )
                }

        if not 1 <= limit <= 50:
            return {'error': 'limit must be between 1 and 50'}
        if offset < 0:
            return {'error': 'offset must be >= 0'}

        model = entity_spec['model']
        query = with_archived(self.db, model, include_archived) if hasattr(model, 'archived_at') else self.db.query(model)
        for key, value in filters.items():
            column = getattr(model, key, None)
            if column is None and key == 'id':
                column = getattr(model, 'key', None)
            if column is not None:
                query = query.filter(column == value)
        rows = query.order_by(entity_spec['order_by']).offset(offset).limit(limit).all()

        serialized = [entity_spec['serialize'](row) for row in rows]
        # A knowledge item's content is only useful in full, so include it on
        # a point lookup (id filter) while list queries stay compact.
        if entity == 'knowledge' and 'id' in filters:
            for row, data in zip(rows, serialized):
                data['content'] = row.content

        return {
            'status': 'success',
            'entity': entity,
            'count': len(rows),
            'limit': limit,
            'offset': offset,
            'rows': serialized,
        }

    async def _handle_create_task(self, args: str, session_id: str) -> dict:
        from datetime import datetime
        from sqlalchemy import update as sa_update

        # Parse args: 'task title --project name --depends-on id1,id2'
        depends_on: list[str] = []
        if '--depends-on' in args:
            args, dep_part = args.split('--depends-on', 1)
            dep_part = dep_part.strip().split()[0] if dep_part.strip() else ''
            depends_on = [dep_id for dep_id in dep_part.split(',') if dep_id]

        project = None
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

        # Atomic per-project counter: UPDATE ... SET seq = seq + 1 is race-safe
        # under concurrent callers on both SQLite and Postgres, unlike a
        # read-then-write COUNT(*) (which both races and reuses IDs after a
        # task is deleted).
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

        task = Task(
            id=task_id,
            title=title,
            project=project,
            status='todo',
            current_gate='spec',
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        task.mode = TaskOrchestrationService(self.db).mode_for_task(task)
        self.db.add(task)
        project_row.next_task_seq = seq + 1
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
        from app.db.models import Agent
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
            return {'error': str(exc)}

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
            # Record the broker message id so the outbox publisher knows this
            # run was already sent — a NULL id makes it publish a duplicate.
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

        service = TaskOrchestrationService(self.db)
        try:
            result = service.request_review(
                task_id=task_id,
                reviewer=reviewer,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(
                    session_id,
                    "request_review",
                    args,
                    attempt=self._dispatch_attempt(task_id),
                ),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

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
            # See _handle_dispatch_task: without the message id the outbox
            # publisher re-sends this run as a duplicate.
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

    async def _handle_generate_spec_plan(self, args: str, session_id: str) -> dict:
        from app.services.agent_suggester import AgentSuggester
        from app.services.spec_plan_generator import (
            SpecPlanGenerationError,
            generate_spec_plan,
        )

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /spec-plan <task_id> [agent_id]'}

        task_id = parts[0]
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        agent_id = parts[1] if len(parts) > 1 else None
        if agent_id:
            agent = self.db.get(Agent, agent_id)
            if agent is None:
                return {'error': f'Agent {agent_id} not found'}
        else:
            suggestions = AgentSuggester(self.db).suggest(task, role="spec_plan", top_n=1)
            if not suggestions:
                return {'error': 'No suitable agent found for spec/plan generation'}
            agent = self.db.get(Agent, suggestions[0].agent_id)

        repo_root, _error = self._research_repo_root(session_id)

        # Feed the planner the same project context executors get — a
        # planner that ignores the repo's conventions plans against them.
        project = self.db.get(Project, task.project) if task.project else None
        context_parts: list[str] = []
        if project is not None and (project.context_md or '').strip():
            context_parts.append(project.context_md.strip())
        if project is not None:
            from app.services.context_generator import get_matching_rules

            for rule in get_matching_rules(self.db, project.id, task.files or None):
                context_parts.append(f"## Rule: {rule.name}\n{rule.content}")
        project_context = "\n\n".join(context_parts) or None

        try:
            result, flows = await generate_spec_plan(
                task, repo_root, agent, project_context=project_context
            )
        except (SpecPlanGenerationError, ConfigurationError) as exc:
            return {'error': str(exc)}

        service = TaskOrchestrationService(self.db)

        try:
            updated = service.write_spec_plan(
                task_id=task_id,
                actor=f"chat:{session_id or 'anonymous'}",
                acceptance_criteria=result.acceptance_criteria,
                plan=result.plan,
                files=result.files,
                tests=result.tests,
                risk=result.risk,
                flows=flows,
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        return {
            'action': 'spec_plan_generated',
            'task_id': task_id,
            'acceptance_criteria': updated.acceptance_criteria,
            'plan': updated.plan,
            'files': updated.files,
            'tests': updated.tests,
            'risk': updated.risk,
            'flows': updated.flows,
            'repo_root': repo_root,
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
            return {'error': str(exc)}

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
        self.db.refresh(task)
        return {'action': 'cancelled', 'task_id': task_id, 'run_id': run.id, 'status': 'cancelled',
                'task': self._task_snapshot(task)}

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
                # The verdict's authorized reviewer identity is established by
                # TaskOrchestrationService itself (from the completed review
                # AgentRun), never by trusting a caller-supplied actor here —
                # see CTV2-087.
                actor=f"chat:{session_id or 'anonymous'}",
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
            # Look up task_id from gate record
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
                # See _handle_dispatch_task: without the message id the
                # outbox publisher re-sends this run as a duplicate.
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
            # The REST endpoint nudges the orchestration driver after an
            # approval. Native MCP approvals must wake it as well; the
            # idempotent driver safely ignores a duplicate run enqueue.
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
        return {
            'action': 'gate_decision',
            'task_id': result.task.id,
            'decision': result.status,
            'new_status': result.task.status,
            'run_id': run.id if run is not None else None,
            'nudged': nudged,
            'task': self._task_snapshot(result.task),
        }

    async def _decide_admin_gate(
        self, raw_id: str, decision: str, session_id: str
    ) -> dict:
        try:
            admin_gate_id = int(raw_id.split(':', 1)[1])
        except (IndexError, ValueError):
            return {'error': 'Invalid admin gate record id'}
        try:
            result = AdminGateService(self.db).decide(
                admin_gate_id=admin_gate_id,
                decision=decision,
                actor=f"chat:{session_id or 'anonymous'}",
            )
        except AdminOrchestrationError as exc:
            return {'error': str(exc)}
        return {
            'action': 'admin_gate_decision',
            'entity': result.record.entity,
            'entity_id': result.entity_id,
            'decision': result.record.status,
        }

    async def _handle_manage_project(self, args: str, session_id: str) -> dict:
        return await self._manage_admin_entity('projects', args, session_id)

    async def _handle_manage_agent(self, args: str, session_id: str) -> dict:
        return await self._manage_admin_entity('agents', args, session_id)

    async def _manage_admin_entity(
        self, entity: str, args: str, session_id: str
    ) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': f'Invalid manage_{entity[:-1]} payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}
        if entity == 'agents' and 'api_key' in payload:
            # Encrypt before the payload reaches AdminGateService: the gate
            # ledger is append-only, so a plaintext key stored there would be
            # unredactable forever. Only the ciphertext may be persisted.
            payload = dict(payload)
            raw_key = payload.pop('api_key')
            if raw_key:
                payload['api_key_encrypted'] = encrypt_api_key(str(raw_key))

        action = str(payload.get('action', '')).strip()
        if not action:
            return {'error': 'action is required'}
        raw_id = payload.get('id')
        entity_id = str(raw_id).strip() if raw_id else None
        mode = str(payload.get('mode') or 'supervised').strip()
        # 'id' stays in the payload (create needs it as the new entity's id;
        # update/archive/disable ignore it since it isn't a settable field).
        mutation_fields = {k: v for k, v in payload.items() if k not in ('action', 'mode')}

        try:
            result = AdminGateService(self.db).request(
                entity=entity,
                action=action,
                entity_id=entity_id,
                payload=mutation_fields,
                actor=f"chat:{session_id or 'anonymous'}",
                mode=mode,
            )
        except (AdminOrchestrationError, entity_admin.EntityError) as exc:
            return {'error': str(exc)}

        if not result.applied:
            return {
                'action': f'{entity}_pending',
                'admin_gate_record_id': f'admin:{result.record.id}',
                'status': 'pending',
            }
        return {
            'action': f'{entity}_{action}d',
            **(result.output or {}),
        }

    async def _handle_update_settings(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid update_settings payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}

        key = str(payload.get('key', '')).strip()
        if not key:
            return {'error': 'key is required'}
        if 'value' not in payload:
            return {'error': 'value is required'}
        if key not in entity_admin.SETTINGS_WHITELIST:
            return {
                'error': (
                    f"Unknown setting key '{key}'. Allowed keys: "
                    f"{', '.join(sorted(entity_admin.SETTINGS_WHITELIST))}"
                )
            }
        mode = str(payload.get('mode') or 'supervised').strip()

        try:
            result = AdminGateService(self.db).request(
                entity='settings',
                action='update',
                entity_id=key,
                payload={'value': payload['value']},
                actor=f"chat:{session_id or 'anonymous'}",
                mode=mode,
            )
        except (AdminOrchestrationError, entity_admin.EntityError) as exc:
            return {'error': str(exc)}

        if not result.applied:
            return {
                'action': 'settings_pending',
                'admin_gate_record_id': f'admin:{result.record.id}',
                'status': 'pending',
            }
        return {
            'action': 'settings_updated',
            **(result.output or {}),
        }

    async def _handle_manage_knowledge(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid manage_knowledge payload'}
        if not isinstance(payload, Mapping):
            return {'error': 'Payload must be a JSON object'}

        action = str(payload.get('action', '')).strip()
        raw_id = payload.get('id')
        item_id = str(raw_id).strip() if raw_id else None
        fields = {k: v for k, v in payload.items() if k not in ('action', 'id')}
        actor = f"chat:{session_id or 'anonymous'}"

        try:
            if action == 'create':
                create_fields = dict(fields)
                if item_id:
                    create_fields['id'] = item_id
                item = entity_admin.create_knowledge(self.db, create_fields)
            elif action == 'update':
                if not item_id:
                    return {'error': 'id is required for update'}
                item = entity_admin.update_knowledge(self.db, item_id, fields)
            elif action in {'archive', 'restore'}:
                if not item_id:
                    return {'error': f'id is required for {action}'}
                try:
                    service = ArchiveService(self.db, actor)
                    service_result = service.archive('knowledge', item_id) if action == 'archive' else service.restore('knowledge', item_id)
                except ArchiveError as exc:
                    return {'error': str(exc)}
                item = self.db.get(KnowledgeItem, item_id)
            else:
                return {
                    'error': (
                        f"Unknown action '{action}'. Valid actions: "
                        "create, update, archive, restore"
                    )
                }
        except entity_admin.EntityError as exc:
            return {'error': str(exc)}

        self.db.add(
            AuditLog(
                task_id=None,
                action=f'manage_knowledge:{action}',
                actor=actor,
                details={'id': item.id, 'archive_result': locals().get('service_result')},
            )
        )
        self.db.commit()
        return {
            'action': f'knowledge_{action}d',
            'id': item.id,
            'title': item.title,
            'status': item.status,
        }

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
        return {
            'action': 'updated',
            'task_id': task.id,
            'plan': task.plan,
            'acceptance_criteria': task.acceptance_criteria,
            'priority': task.priority,
            'tags': task.tags,
            'depends_on': depends_on,
        }

    @staticmethod
    def _command_key(session_id: str, action: str, args: str, attempt: int = 1) -> str:
        # The attempt discriminator must survive truncation, so the 100-char
        # cap is applied to the session-id prefix *before* the fixed suffix
        # (action/digest/attempt) is appended — never after, or a long
        # session_id could push the attempt number off the end and collide
        # two different attempts onto the same key.
        digest = hashlib.sha256(args.strip().encode("utf-8")).hexdigest()[:24]
        suffix = f":{action}:{digest}:{attempt}"
        prefix = f"chat:{session_id or 'anonymous'}"[: max(0, 100 - len(suffix))]
        return f"{prefix}{suffix}"[:100]

    def _dispatch_attempt(self, task_id: str) -> int:
        """Number of times this task has already had a run created for it.

        Deterministic from persisted state (not a timestamp/random nonce) so
        the same coordinator retry always maps to a stable, resumable key —
        while a genuinely new attempt (issued after a prior run exists) gets
        a fresh idempotency key instead of colliding with a stale one.
        """
        existing_runs = (
            self.db.query(AgentRun).filter(AgentRun.task_id == task_id).count()
        )
        return existing_runs + 1

    def _approve_attempt(self, task_id: str) -> int:
        """Number of gate decisions already made for this task.

        Used to generate unique idempotency keys for approve_gate retries,
        allowing users to change agent or retry after a crash while keeping
        full history of all attempts.
        """
        existing_decisions = (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.status.in_(["approved", "rejected"]),
            )
            .count()
        )
        return existing_decisions + 1

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
        compacted = ctx.compact_context(
            session,
            # An explicit /compact request is a deliberate override of the
            # automatic token threshold; automatic coordinator turns still
            # use the model-window ratio.
            threshold=0,
        )
        return {'action': 'compacted', 'session_id': session_id, 'compacted': compacted}
