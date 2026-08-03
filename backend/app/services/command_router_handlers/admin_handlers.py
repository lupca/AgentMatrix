import json
import logging
from collections.abc import Mapping

from sqlalchemy import exists, or_

from app.db.models import (
    AgentRun,
    LLMUsage,
    RunResourceUsage,
    Task,
)
from app.services import entity_admin
from app.services.admin_gate import AdminGateService, AdminOrchestrationError
from app.services.agent_suggester import AgentSuggester
from app.services.crypto import encrypt_api_key
from app.services.tool_registry import (
    DEFERRED_GROUPS,
    dump_registry,
    get_group_tool_definitions,
)

logger = logging.getLogger(__name__)

HELP_COMMAND = {
    'name': 'help',
    'description': 'List available commands and tools.',
    'slash_alias': '/help',
    'tier': 'eager',
    'group': 'meta',
}


class AdminHandlersMixin:
    async def _handle_show_help(self, args: str, session_id: str) -> dict:
        return {'commands': dump_registry() + [HELP_COMMAND]}

    async def _handle_get_stats(self, args: str, session_id: str) -> dict:
        try:
            payload = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {'error': 'Invalid get_stats arguments'}
        task_id = str(payload.get('task_id') or '').strip() or None
        agent_id = str(payload.get('agent_id') or '').strip() or None
        usage_query = self.db.query(LLMUsage)
        if task_id or agent_id:
            usage_query = usage_query.outerjoin(
                AgentRun, LLMUsage.agent_run_id == AgentRun.id
            )
        if task_id:
            usage_query = usage_query.filter(
                or_(LLMUsage.task_id == task_id, AgentRun.task_id == task_id)
            )
        if agent_id:
            usage_query = usage_query.filter(AgentRun.agent_id == agent_id)
        usage = usage_query.all()
        cli_runs = self.db.query(AgentRun)
        if task_id:
            cli_runs = cli_runs.filter(AgentRun.task_id == task_id)
        if agent_id:
            cli_runs = cli_runs.filter(AgentRun.agent_id == agent_id)
        resources = self.db.query(RunResourceUsage)
        if task_id or agent_id:
            resources = resources.join(AgentRun, RunResourceUsage.agent_run_id == AgentRun.id)
            if task_id:
                resources = resources.filter(AgentRun.task_id == task_id)
            if agent_id:
                resources = resources.filter(AgentRun.agent_id == agent_id)
        resource_rows = resources.all()
        measured_calls = len(usage)
        has_usage = exists().where(LLMUsage.agent_run_id == AgentRun.id)
        unmeasured_cli_runs = cli_runs.filter(~has_usage).count()
        unpriced_cli_usage = [
            row for row in usage if str(row.provider or "").lower() in {"qwen", "agy"}
        ]
        known_cost_usd = sum(
            float(row.cost_usd or 0)
            for row in usage
            if row not in unpriced_cli_usage
        )
        if measured_calls and unmeasured_cli_runs:
            cost_status = 'partial'
            cost_note = (
                'Some usage is measured, but at least one CLI run has no parsed token usage.'
            )
        elif measured_calls:
            cost_status = 'measured'
            cost_note = 'Token usage was parsed from CLI JSON output.'
        elif unmeasured_cli_runs:
            cost_status = 'unmeasured'
            cost_note = (
                'No CLI token usage was parsed; zero is not a cost estimate.'
            )
        else:
            cost_status = 'no_data'
            cost_note = 'No recorded API usage or completed CLI run is available for this scope.'
        if unpriced_cli_usage:
            cost_note += (
                ' Qwen/Agy expose measured tokens but no authoritative USD cost; '
                'cost_usd excludes those runs.'
            )
        cost_value = known_cost_usd
        if unpriced_cli_usage and cost_value == 0:
            cost_value = None
        run_cost_value = sum(float(row.estimated_cost_usd or 0) for row in resource_rows)
        if unpriced_cli_usage and run_cost_value == 0 and known_cost_usd == 0:
            run_cost_value = None
        return {
            'task_id': task_id,
            'agent_id': agent_id,
            'calls': len(usage),
            'input_tokens': sum(row.input_tokens or 0 for row in usage),
            'output_tokens': sum(row.output_tokens or 0 for row in usage),
            'cached_tokens': sum(row.cached_tokens or 0 for row in usage),
            'cost_usd': round(cost_value, 8) if cost_value is not None else None,
            'cost_status': cost_status,
            'cost_scope': (
                'recorded_usage' if measured_calls else 'recorded_api_usage_only'
            ),
            'unmeasured_cli_runs': unmeasured_cli_runs,
            'cost_note': cost_note,
            'runs': len(resource_rows),
            'run_cost_usd': round(run_cost_value, 8)
            if run_cost_value is not None
            else None,
        }

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
