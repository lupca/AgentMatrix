import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Optional, Tuple

from app.services.tool_registry import (
    TOOL_REGISTRY,
    resolve_tool_name,
)
from app.services.graph_client import (
    get_impact_radius as graph_get_impact_radius,
    semantic_search,
)
from app.services.embedding import EmbeddingError, embed_text
from app.services.command_router_handlers import (
    AdminHandlersMixin,
    ContextHandlersMixin,
    ImplDesignHandlersMixin,
    QueryHandlersMixin,
    SpecHandlersMixin,
    TaskHandlersMixin,
    _QUERY_DB_ENTITIES,
    _coerce_filter_value,
)

logger = logging.getLogger(__name__)

COMMANDS = {
    spec.slash_alias: spec.handler
    for spec in TOOL_REGISTRY.values()
    if spec.slash_alias
}
COMMANDS['/help'] = 'show_help'

HELP_COMMAND = {
    'name': 'help',
    'description': 'List available commands and tools.',
    'slash_alias': '/help',
    'tier': 'eager',
    'group': 'meta',
}


class CommandRouter(
    TaskHandlersMixin,
    QueryHandlersMixin,
    ContextHandlersMixin,
    ImplDesignHandlersMixin,
    AdminHandlersMixin,
    SpecHandlersMixin,
):
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
        elif canonical_name == 'manage_inbox':
            action = str(args.get('action', '')).strip().lower()
            if not action:
                return {'error': 'action is required'}
            command_args = json.dumps(args, ensure_ascii=False)
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
            command_args = f'{task_id} {verdict}'
        elif canonical_name == 'attach_result':
            task_id = str(args.get('task_id', '')).strip()
            commit = str(args.get('commit') or args.get('result_ref') or '').strip()
            if not task_id or not commit:
                return {'error': 'task_id and commit are required'}
            command_args = json.dumps({
                'task_id': task_id,
                'commit': commit,
                'option': str(args.get('option', 'request_review')).strip(),
            }, ensure_ascii=False)
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
            critic_id = str(args.get('critic_id', '') or '').strip()
            if critic_id and not agent_id:
                return {'error': 'agent_id is required when critic_id is provided'}
            command_args = ' '.join(
                part for part in (task_id, agent_id, critic_id) if part
            )
        elif canonical_name == 'critique_spec_plan':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = json.dumps({
                'task_id': task_id,
                'critic_id': str(args.get('critic_id', '') or '').strip(),
            }, ensure_ascii=False)
        elif canonical_name == 'approve_gate':
            gate_id = args.get('gate_record_id', args.get('task_id'))
            if gate_id is None:
                return {'error': 'gate_record_id is required'}
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
        elif canonical_name == 'manage_notes':
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
            command_args = json.dumps({
                'file': file,
                'max_depth': args.get('max_depth', 2),
            }, ensure_ascii=False)
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
        elif canonical_name == 'spec_write':
            ops = args.get('ops')
            if not isinstance(ops, list):
                return {'error': 'ops must be an array'}
            command_args = json.dumps({
                'ops': ops,
                'project_id': args.get('project_id'),
            }, ensure_ascii=False)
        elif canonical_name == 'spec_get':
            ids = args.get('ids')
            filters = args.get('filter', args.get('filters'))
            task_id = args.get('task_id')
            if ids is not None and not isinstance(ids, list):
                return {'error': 'ids must be an array'}
            if filters is not None and not isinstance(filters, Mapping):
                return {'error': 'filter must be an object'}
            command_args = json.dumps({
                'ids': ids,
                'filter': filters,
                'task_id': task_id,
            }, ensure_ascii=False)
        elif canonical_name == 'spec_stale':
            project = str(args.get('project', '')).strip()
            if not project:
                return {'error': 'project is required'}
            command_args = project
        elif canonical_name == 'impl_design':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            action = str(args.get('action', 'get')).strip().lower()
            command_args = json.dumps({**args, 'action': action, 'task_id': task_id}, ensure_ascii=False)
        else:
            return {'error': f'Unknown tool: {tool_name}'}

        return await self.execute(spec.handler, command_args, session_id)

    @staticmethod
    def _command_key(session_id: str, action: str, args: str, attempt: int = 1) -> str:
        digest = hashlib.sha256(args.strip().encode("utf-8")).hexdigest()[:24]
        suffix = f":{action}:{digest}:{attempt}"
        prefix = f"chat:{session_id or 'anonymous'}"[: max(0, 100 - len(suffix))]
        return f"{prefix}{suffix}"[:100]
