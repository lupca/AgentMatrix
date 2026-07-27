import re
import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any, Tuple, Optional
from app.db.models import (
    Agent,
    AgentRun,
    AuditLog,
    GateRecord,
    KnowledgeItem,
    LLMUsage,
    Project,
    Setting,
    Task,
    Session as SessionModel,
)
from app.services import entity_admin
from app.services.admin_gate import AdminGateService, AdminOrchestrationError
from app.services.task_orchestration import (
    OrchestrationError,
    TaskOrchestrationService,
)
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
        "filters": {"status": "str"},
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
        "filters": {"category": "str", "project": "str", "author": "str"},
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
        "filters": {},
        "order_by": Setting.key.asc(),
        "serialize": lambda s: {
            "key": s.key,
            "value": s.value,
            "description": s.description,
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
        elif canonical_name == 'query_db':
            entity = str(args.get('entity', '')).strip()
            if not entity:
                return {'error': 'entity is required'}
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
            executor = str(args.get('executor', '') or '').strip()
            command_args = ' '.join(part for part in (task_id, executor) if part)
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
            reviewer = str(args.get('reviewer', '') or '').strip()
            command_args = ' '.join(part for part in (task_id, reviewer) if part)
        elif canonical_name == 'generate_spec_plan':
            task_id = str(args.get('task_id', '')).strip()
            if not task_id:
                return {'error': 'task_id is required'}
            command_args = task_id
        elif canonical_name == 'approve_gate':
            gate_id = args.get('gate_record_id', args.get('task_id'))
            if gate_id is None:
                return {'error': 'gate_record_id is required'}
            command_args = str(gate_id)
        elif canonical_name == 'cancel_task':
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
        else:
            return {'error': f'Unknown tool: {tool_name}'}

        return await self.execute(spec.handler, command_args, session_id)
    
    async def _handle_show_help(self, args: str, session_id: str) -> dict:
        return {'commands': dump_registry() + [HELP_COMMAND]}

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

    async def _handle_query_db(self, args: str, session_id: str) -> dict:
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
        query = self.db.query(model)
        for key, value in filters.items():
            query = query.filter(getattr(model, key) == value)
        rows = query.order_by(entity_spec['order_by']).offset(offset).limit(limit).all()

        return {
            'status': 'success',
            'entity': entity,
            'count': len(rows),
            'limit': limit,
            'offset': offset,
            'rows': [entity_spec['serialize'](row) for row in rows],
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
                    session_id,
                    "dispatch",
                    args,
                    attempt=self._dispatch_attempt(task_id),
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
            suggestions = AgentMatcher(self.db).suggest_agents(task, top_n=5)
            independent = [
                s for s in suggestions
                if (task.executor or '').strip().casefold() != s.agent_id.strip().casefold()
            ]
            if not independent:
                return {
                    'error': (
                        f'No independent reviewer available for task {task_id} '
                        f'(executor={task.executor!r}); refusing to lower the '
                        'four-eyes bar.'
                    ),
                    'reason': 'no_independent_reviewer',
                }
            reviewer = independent[0].agent_id

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
            }

        run = result.agent_run
        context = result.context or {}
        if run is None:
            return {'error': 'Review transition did not create an agent run'}
        try:
            run_agent.send(
                run.id,
                task_id,
                run.command,
                context['repo_root'],
                run.timeout_seconds,
            )
        except Exception as exc:
            error = f'Could not queue review run: {exc}'
            service.record_dispatch_queue_failure(
                run_id=run.id,
                error=error,
                actor='system:dispatch-queue',
                idempotency_key=f'{result.gate_record.idempotency_key}:queue-failure',
            )
            return {'error': error, 'run_id': run.id}

        return {
            'action': 'review_requested',
            'task_id': task_id,
            'run_id': run.id,
            'reviewer': reviewer,
        }

    async def _handle_generate_spec_plan(self, args: str, session_id: str) -> dict:
        from app.services.spec_plan_generator import (
            SpecPlanGenerationError,
            generate_spec_plan,
        )

        task_id = args.strip()
        if not task_id:
            return {'error': 'Usage: /spec-plan <task_id>'}

        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}

        service = TaskOrchestrationService(self.db)
        try:
            gate_result = service.request_spec_plan_model(
                task_id=task_id,
                actor=f"chat:{session_id or 'anonymous'}",
                idempotency_key=self._command_key(
                    session_id, "spec_plan_model", args,
                    attempt=self._spec_plan_model_attempt(task_id),
                ),
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        if not gate_result.applied:
            return {
                'action': 'spec_plan_model_pending',
                'task_id': task_id,
                'gate_record_id': gate_result.gate_record.id,
                'status': 'pending',
                'suggested_model': (gate_result.context or {}).get('model_config'),
            }

        model_config = dict((gate_result.context or {}).get('model_config') or {})
        agent_id = model_config.get("agent_id")
        if agent_id:
            model_config["agent"] = self.db.get(Agent, agent_id)

        repo_root, _error = self._research_repo_root(session_id)

        try:
            result, flows = await generate_spec_plan(task, repo_root, model_config)
        except (SpecPlanGenerationError, ConfigurationError) as exc:
            return {'error': str(exc)}

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
        }

    async def _handle_approve_gate(self, args: str, session_id: str) -> dict:
        from app.workers.agent_runner import run_agent

        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /approve <gate_record_id> [approved|rejected]'}
        raw_id = parts[0]
        decision = parts[1].lower() if len(parts) > 1 else 'approved'

        if raw_id.startswith('admin:'):
            return await self._decide_admin_gate(raw_id, decision, session_id)

        try:
            gate_record_id = int(raw_id)
        except ValueError:
            pending = (
                self.db.query(GateRecord)
                .filter(
                    GateRecord.task_id == raw_id,
                    GateRecord.status == "pending",
                )
                .order_by(GateRecord.id.desc())
                .first()
            )
            if pending is None:
                return {'error': f'No pending gate found for task {raw_id}'}
            gate_record_id = pending.id
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
            return {
                'error': (
                    'manage_agent cannot accept an api_key value. Configure '
                    'API-agent credentials through the REST API '
                    '(POST/PATCH /api/agents) instead.'
                )
            }

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
            elif action == 'archive':
                if not item_id:
                    return {'error': 'id is required for archive'}
                item = entity_admin.archive_knowledge(self.db, item_id)
            else:
                return {
                    'error': (
                        f"Unknown action '{action}'. Valid actions: "
                        "create, update, archive"
                    )
                }
        except entity_admin.EntityError as exc:
            return {'error': str(exc)}

        self.db.add(
            AuditLog(
                task_id=None,
                action=f'manage_knowledge:{action}',
                actor=actor,
                details={'id': item.id},
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

        try:
            task = TaskOrchestrationService(self.db).update_task_fields(
                task_id=task_id,
                patch=dict(patch),
                actor=f"chat:{session_id or 'anonymous'}",
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        return {
            'action': 'updated',
            'task_id': task.id,
            'plan': task.plan,
            'acceptance_criteria': task.acceptance_criteria,
            'priority': task.priority,
            'tags': task.tags,
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

    def _spec_plan_model_attempt(self, task_id: str) -> int:
        """Number of times this task has already requested a spec/plan model gate.

        Same rationale as ``_dispatch_attempt``: derived from persisted state
        so retries get a fresh idempotency key instead of colliding with a
        stale one.
        """
        existing = (
            self.db.query(GateRecord)
            .filter(
                GateRecord.task_id == task_id,
                GateRecord.gate_type == "spec_plan_model",
            )
            .count()
        )
        return existing + 1

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
        compacted = ctx.compact_context(
            session,
            # An explicit /compact request is a deliberate override of the
            # automatic token threshold; automatic coordinator turns still
            # use the model-window ratio.
            threshold=0,
        )
        return {'action': 'compacted', 'session_id': session_id, 'compacted': compacted}
