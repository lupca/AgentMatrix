import json
import os
import sys
from typing import Any
from collections.abc import Mapping

from app.db.models import (
    Agent,
    Project,
    Task,
    Session as SessionModel,
)
from app.services import spec_plan_generator
from app.services.agent_suggester import AgentSuggester
from app.services.context_hierarchy import ContextHierarchy
from app.services.llm_service import ConfigurationError
from app.services.spec_plan_generator import SpecPlanGenerationError


def _get_graph_get_impact_radius():
    cr_mod = sys.modules.get('app.services.command_router')
    if cr_mod and hasattr(cr_mod, 'graph_get_impact_radius'):
        return cr_mod.graph_get_impact_radius
    from app.services.graph_client import get_impact_radius
    return get_impact_radius


def _get_semantic_search():
    cr_mod = sys.modules.get('app.services.command_router')
    if cr_mod and hasattr(cr_mod, 'semantic_search'):
        return cr_mod.semantic_search
    from app.services.graph_client import semantic_search
    return semantic_search


def _get_record_tool_metric():
    tm_mod = sys.modules.get('app.services.tool_metrics')
    if tm_mod and hasattr(tm_mod, 'record_tool_metric'):
        return tm_mod.record_tool_metric
    from app.services.tool_metrics import record_tool_metric
    return record_tool_metric


class ContextHandlersMixin:
    def _research_task_id(self, session_id: str) -> str | None:
        session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
        return session.task_id if session and session.task_id else None

    def _research_repo_root(self, session_id: str) -> tuple[str | None, dict | None]:
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
        kind = getattr(exc, 'kind', 'unavailable')
        if kind == 'graph_not_built':
            reason = 'graph_not_built'
            suggestion = 'Build the code graph, then retry the research tool.'
        elif kind in {'transport', 'empty_response'}:
            reason = 'graph_transport_error'
            suggestion = 'Retry the request and check the graph MCP transport if it persists.'
        elif kind == 'tool_error':
            reason = 'graph_tool_error'
            suggestion = 'Check the graph MCP tool error and retry after correcting it.'
        else:
            reason = 'graph_unavailable'
            suggestion = 'Build or refresh the code graph, then retry the research tool.'
        return {
            'status': 'error',
            'reason': reason,
            'detail': str(exc),
            'suggestion': suggestion,
        }

    async def _apply_graph_staleness_check(self, repo_root: str, session_id: str, res_dict: dict) -> None:
        try:
            from app.services.graph_client import check_graph_staleness
            from app.services.outbox import record_graph_rebuild_requested

            session = self.db.query(SessionModel).filter(SessionModel.id == session_id).first()
            project_id = session.project_id if session else None
            if session and session.task_id:
                task = self.db.query(Task).filter(Task.id == session.task_id).first()
                project_id = task.project if task else project_id

            if not project_id and repo_root:
                project_obj = self.db.query(Project).filter(Project.repo_root == repo_root).first()
                if project_obj:
                    project_id = project_obj.id

            stale_info = await check_graph_staleness(repo_root)
            if stale_info.get("is_stale"):
                res_dict["graph_stale"] = True
                res_dict["warning"] = stale_info.get("warning") or "graph đang cũ"
                if project_id:
                    project = self.db.query(Project).filter(Project.id == project_id).first()
                    if project:
                        if project.graph_status in (None, "idle", "fresh"):
                            project.graph_status = "stale"
                        record_graph_rebuild_requested(
                            self.db, project.id, repo_root, commit_sha=stale_info.get("head_sha")
                        )
                        self.db.flush()
        except Exception:
            pass

    async def _handle_get_minimal_context(self, args: str, session_id: str) -> dict:
        repo_root, error = self._research_repo_root(session_id)
        if error:
            return error
        try:
            payload = json.loads(args)
            search_fn = _get_semantic_search()
            search_kwargs = {
                'raise_on_error': True,
                'compress_output': True,
            }
            task_id = self._research_task_id(session_id)
            if task_id:
                search_kwargs['task_id'] = task_id
            result = await search_fn(
                repo_root, str(payload['query']), int(payload.get('limit', 10)),
                **search_kwargs,
            )
        except Exception as exc:
            return self._research_error(exc)
        res = {'status': 'success', 'repo_root': repo_root, 'context': result}
        await self._apply_graph_staleness_check(repo_root, session_id, res)
        return res

    async def _handle_get_impact_radius(self, args: str, session_id: str) -> dict:
        repo_root, error = self._research_repo_root(session_id)
        if error:
            return error
        try:
            try:
                payload = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                payload = {'file': args.strip(), 'max_depth': 2}
            impact_fn = _get_graph_get_impact_radius()
            impact_kwargs = {
                'max_depth': int(payload.get('max_depth', 2)),
                'raise_on_error': True,
                'compress_output': True,
            }
            task_id = self._research_task_id(session_id)
            if task_id:
                impact_kwargs['task_id'] = task_id
            result = await impact_fn(
                repo_root, str(payload['file']).strip(), **impact_kwargs
            )
        except Exception as exc:
            return self._research_error(exc)
        res = {'status': 'success', 'repo_root': repo_root, 'files': result}
        await self._apply_graph_staleness_check(repo_root, session_id, res)
        return res

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

    async def _handle_generate_spec_plan(self, args: str, session_id: str) -> dict:
        from app.services.task_orchestration import TaskOrchestrationService, OrchestrationError

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

        project = self.db.get(Project, task.project) if task.project else None
        repo_root = os.path.abspath(project.repo_root) if project and project.repo_root else None
        context_parts: list[str] = []
        if project is not None and (project.context_md or '').strip():
            context_parts.append(project.context_md.strip())
        if project is not None:
            from app.services.context_generator import get_matching_rules

            for rule in get_matching_rules(self.db, project.id, task.files or None):
                context_parts.append(f"## Rule: {rule.name}\n{rule.content}")
        project_context = "\n\n".join(context_parts) or None

        try:
            result, flows = await spec_plan_generator.generate_spec_plan(
                task, repo_root, agent, project_context=project_context
            )
        except (SpecPlanGenerationError, ConfigurationError) as exc:
            return {'error': str(exc)}

        record_metric_fn = _get_record_tool_metric()
        record_metric_fn(
            tool='spec_plan',
            source='spec_plan_generator',
            ok=True,
            task_id=task_id,
            result_count=len(result.open_questions),
            payload={'spec_clarity': result.spec_clarity, 'task_id': task_id},
        )

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
                spec_clarity=result.spec_clarity,
                open_questions=result.open_questions,
            )
        except OrchestrationError as exc:
            return {'error': str(exc)}

        questions_pending = bool(updated.open_questions) or updated.spec_clarity != 'high'
        return {
            'action': 'spec_questions_pending' if questions_pending else 'spec_plan_generated',
            'task_id': task_id,
            'acceptance_criteria': updated.acceptance_criteria,
            'plan': updated.plan,
            'files': updated.files,
            'tests': updated.tests,
            'risk': updated.risk,
            'flows': updated.flows,
            'repo_root': repo_root,
            'spec_clarity': updated.spec_clarity,
            'open_questions': updated.open_questions or [],
            'awaiting_approval': bool(updated.awaiting_approval),
            'approval_prompt': updated.approval_prompt,
        }

    async def _handle_compact_context(self, args: str, session_id: str) -> dict:
        session = self.db.query(SessionModel).filter(
            (SessionModel.id == session_id) | (SessionModel.thread_id == session_id)
        ).first()
        if not session:
            return {'error': f'Session {session_id} not found'}

        ctx = ContextHierarchy(self.db)
        compacted = ctx.compact_context(
            session,
            threshold=0,
        )
        return {'action': 'compacted', 'session_id': session_id, 'compacted': compacted}
