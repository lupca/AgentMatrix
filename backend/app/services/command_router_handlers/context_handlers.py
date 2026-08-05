import asyncio
import json
import os
import sys
import time
from typing import Any
from collections.abc import Mapping

from app.db.models import (
    Agent,
    AgentRun,
    Project,
    Task,
    Session as SessionModel,
)
from app.services.agent_suggester import AgentSuggester
from app.services.context_hierarchy import ContextHierarchy
from app.workers import plan_executor

_GENERATE_SPEC_PLAN_WAIT_SECONDS = 30


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


def _record_research_call(
    *, tool: str, task_id: str | None, started: float, ok: bool, error: str | None = None
) -> None:
    """One row per research-tool call, named after the tool the caller invoked.

    Without this the question CTV2-1340 exists to answer -- "do executors
    actually use the code graph?" -- has no instrument.  `graph_client` records
    under its own internal names, so `get_minimal_context` calls land as
    `semantic_search` in the same bucket as the planner's own searches
    (`spec_plan_generator`), and the two cannot be told apart.  Measured
    2026-08-06: 172 `semantic_search` rows, zero of them attributable.

    `source` carries WHO called: a task-scoped session is an executor, anything
    else is the coordinator.  That is the whole discriminator the question needs.
    """
    from app.services.tool_metrics import record_tool_metric

    record_tool_metric(
        tool=tool,
        source="mcp:executor" if task_id else "mcp:coordinator",
        ok=ok,
        duration_ms=int((time.monotonic() - started) * 1000),
        task_id=task_id,
        error=error,
    )


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
        started = time.monotonic()
        task_id = self._research_task_id(session_id)
        try:
            payload = json.loads(args)
            search_fn = _get_semantic_search()
            search_kwargs = {
                'raise_on_error': True,
                'compress_output': True,
            }
            if task_id:
                search_kwargs['task_id'] = task_id
            result = await search_fn(
                repo_root, str(payload['query']), int(payload.get('limit', 10)),
                **search_kwargs,
            )
        except Exception as exc:
            _record_research_call(
                tool='get_minimal_context', task_id=task_id, started=started,
                ok=False, error=str(exc),
            )
            return self._research_error(exc)
        _record_research_call(
            tool='get_minimal_context', task_id=task_id, started=started, ok=True,
        )
        res = {'status': 'success', 'repo_root': repo_root, 'context': result}
        await self._apply_graph_staleness_check(repo_root, session_id, res)
        return res

    async def _handle_get_impact_radius(self, args: str, session_id: str) -> dict:
        repo_root, error = self._research_repo_root(session_id)
        if error:
            return error
        started = time.monotonic()
        task_id = self._research_task_id(session_id)
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
            if task_id:
                impact_kwargs['task_id'] = task_id
            result = await impact_fn(
                repo_root, str(payload['file']).strip(), **impact_kwargs
            )
        except Exception as exc:
            _record_research_call(
                tool='get_impact_radius', task_id=task_id, started=started,
                ok=False, error=str(exc),
            )
            return self._research_error(exc)
        _record_research_call(
            tool='get_impact_radius', task_id=task_id, started=started, ok=True,
        )
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

    def _spec_plan_response_payload(self, task_id: str, repo_root: str | None) -> dict:
        task = self.db.get(Task, task_id)
        critic_rejected = task.plan_critic_status == 'reject'
        questions_pending = bool(task.open_questions) or task.spec_clarity != 'high'
        return {
            'action': (
                'spec_plan_critic_rejected'
                if critic_rejected
                else ('spec_questions_pending' if questions_pending else 'spec_plan_generated')
            ),
            'task_id': task_id,
            'acceptance_criteria': task.acceptance_criteria,
            'constraints': task.constraints,
            'evidence': task.evidence,
            'prior_art': task.prior_art,
            'ruled_out': task.ruled_out,
            'limits': task.limits,
            'plan': task.plan,
            'files': task.files,
            'tests': task.tests,
            'risk': task.risk,
            'flows': task.flows,
            'repo_root': repo_root,
            'spec_clarity': task.spec_clarity,
            'open_questions': task.open_questions or [],
            'awaiting_approval': bool(task.awaiting_approval),
            'approval_prompt': task.approval_prompt,
            'planner': task.planner,
            'plan_critic': task.plan_critic,
            'plan_critic_status': task.plan_critic_status,
            'plan_critic_findings': task.plan_critic_findings,
        }

    @staticmethod
    def _run_handle(run: AgentRun, task_id: str) -> dict:
        return {
            'run_id': run.id,
            'task_id': task_id,
            'status': run.status,
            'next': 'wait_for_task',
            'latest_run': run.id,
        }

    def _latest_critic_run(self, task_id: str) -> AgentRun | None:
        return (
            self.db.query(AgentRun)
            .filter(
                AgentRun.task_id == task_id,
                AgentRun.idempotency_key.like(f'{plan_executor.PLANNER_PREFIX}{task_id}:critic:%'),
            )
            .order_by(AgentRun.created_at.desc())
            .first()
        )

    async def _await_spec_plan(
        self, task_id: str, plan_run_id: str, repo_root: str | None, *, timeout_seconds: int,
    ) -> dict:
        """Poll for the (chained) plan -> critic run pair to finish, capped
        at ``timeout_seconds`` total. CTV2-1382: both steps now run in the
        Dramatiq worker, dispatched through the outbox -- this replaces the
        synchronous in-process await that used to hold the MCP request open
        for the full 170-420s of both LLM calls."""

        deadline = time.monotonic() + timeout_seconds
        while True:
            self.db.rollback()
            plan_run = self.db.get(AgentRun, plan_run_id)
            if plan_run is None:
                return {'error': f'AgentRun {plan_run_id} not found'}

            if plan_run.status == 'failed':
                return {
                    'error': plan_run.error_message or 'Spec/plan generation failed',
                    'task_id': task_id,
                }
            if plan_run.status == 'success':
                critic_run = self._latest_critic_run(task_id)
                if critic_run is not None:
                    if critic_run.status == 'failed':
                        return {
                            'error': critic_run.error_message or 'Plan critic failed',
                            'task_id': task_id,
                            'plan_persisted': True,
                            'next': 'critique_spec_plan',
                        }
                    if critic_run.status == 'success':
                        return self._spec_plan_response_payload(task_id, repo_root)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                handle_run = self._latest_critic_run(task_id) or plan_run
                return self._run_handle(handle_run, task_id)
            await asyncio.sleep(min(1.0, remaining))

    async def _handle_generate_spec_plan(self, args: str, session_id: str) -> dict:
        parts = args.strip().split()
        if not parts:
            return {'error': 'Usage: /spec-plan <task_id> [agent_id]'}

        task_id = parts[0]
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}
        if task.status != 'todo':
            return {
                'error': (
                    f"generate_spec_plan requires task status 'todo'; task {task_id} is "
                    f"{task.status!r}"
                )
            }

        agent_id = parts[1] if len(parts) > 1 else None
        critic_id = parts[2] if len(parts) > 2 else None
        if agent_id:
            agent = self.db.get(Agent, agent_id)
            if agent is None:
                return {'error': f'Agent {agent_id} not found'}
        else:
            suggestions = AgentSuggester(self.db).suggest(task, role="spec_plan", top_n=1)
            if not suggestions:
                return {'error': 'No suitable agent found for spec/plan generation'}
            agent = self.db.get(Agent, suggestions[0].agent_id)

        agent_type = getattr(getattr(agent, "agent_type", None), "value", None) or getattr(
            agent, "agent_type", ""
        )
        if str(agent_type).strip().lower() == "api":
            return {
                'error': (
                    "Spec/plan research requires a CLI agent that can read the repository; "
                    f"{agent.id} is API-backed."
                )
            }

        if critic_id:
            critic_agent = self.db.get(Agent, critic_id)
            if critic_agent is None:
                return {'error': f'Critic agent {critic_id} not found'}
            critic_type = getattr(
                getattr(critic_agent, "agent_type", None), "value", None
            ) or getattr(critic_agent, "agent_type", "")
            if str(critic_agent.id).strip().casefold() == str(agent.id).strip().casefold():
                return {'error': 'Plan critic must differ from the planner (four-eyes).'}
            if str(critic_type).strip().lower() == 'api':
                return {'error': 'Plan criticism requires a CLI agent'}
        else:
            # Cheap fail-fast check only -- do NOT pin a specific candidate.
            # The actual critic is selected fresh by the worker once the plan
            # step finishes (minutes later), when agent availability may have
            # changed; see plan_executor._dispatch_critic_step.
            suggestions = AgentSuggester(self.db).suggest(
                task, role="reviewer", top_n=10, exclude_agent_id=agent.id
            )
            has_cli_candidate = False
            for suggestion in suggestions:
                candidate = self.db.get(Agent, suggestion.agent_id)
                if candidate is None:
                    continue
                candidate_type = getattr(
                    getattr(candidate, "agent_type", None), "value", None
                ) or getattr(candidate, "agent_type", "")
                if str(candidate_type).strip().lower() != "api":
                    has_cli_candidate = True
                    break
            if not has_cli_candidate:
                return {'error': 'No independent CLI plan critic is available'}

        project = self.db.get(Project, task.project) if task.project else None
        repo_root = os.path.abspath(project.repo_root) if project and project.repo_root else None
        if not repo_root:
            return {
                'error': 'Spec/plan research requires a configured project repo_root for the CLI agent.'
            }

        run = plan_executor.create_plan_run(self.db, task, agent=agent, step="plan")
        from app.services.task_event_service import emit_task_event

        emit_task_event(
            task_id=task_id,
            event_type=plan_executor.DISPATCH_CONTEXT_EVENT,
            kind="info",
            payload={"critic_id": critic_id or "", "planner_agent_id": agent.id},
            db=self.db,
        )
        plan_executor.dispatch_plan_run(self.db, run, repo_root)

        return await self._await_spec_plan(
            task_id, run.id, repo_root, timeout_seconds=_GENERATE_SPEC_PLAN_WAIT_SECONDS,
        )

    async def _handle_critique_spec_plan(self, args: str, session_id: str) -> dict:
        """Run the plan critic alone against whatever plan is already on the
        task. Never calls the planner — a re-critique round after a rejected
        verdict, or after a critic-only failure, costs only this step."""

        try:
            payload = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            return {'error': 'critique_spec_plan expects a JSON object'}

        task_id = str(payload.get('task_id', '')).strip()
        if not task_id:
            return {'error': 'task_id is required'}
        task = self.db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return {'error': f'Task {task_id} not found'}
        if not task.planner:
            return {'error': f'Task {task_id} has no plan to critique yet — run generate_spec_plan first'}

        critic_id = str(payload.get('critic_id', '') or '').strip()
        if critic_id:
            critic_agent = self.db.get(Agent, critic_id)
            if critic_agent is None:
                return {'error': f'Critic agent {critic_id} not found'}
            critic_type = getattr(
                getattr(critic_agent, "agent_type", None), "value", None
            ) or getattr(critic_agent, "agent_type", "")
            if str(critic_agent.id).strip().casefold() == str(task.planner).strip().casefold():
                return {'error': 'Plan critic must differ from the planner (four-eyes).'}
            if str(critic_type).strip().lower() == 'api':
                return {'error': 'Plan criticism requires a CLI agent'}
        else:
            critic_agent = None
            suggestions = AgentSuggester(self.db).suggest(
                task, role="reviewer", top_n=10, exclude_agent_id=task.planner
            )
            for suggestion in suggestions:
                candidate = self.db.get(Agent, suggestion.agent_id)
                candidate_type = getattr(getattr(candidate, "agent_type", None), "value", None) or getattr(
                    candidate, "agent_type", ""
                )
                if candidate is not None and str(candidate_type).lower() != "api":
                    critic_agent = candidate
                    break
            if critic_agent is None:
                return {'error': 'No independent CLI plan critic is available'}

        planner_agent = self.db.get(Agent, task.planner)
        if planner_agent is None:
            return {'error': f'Planner agent {task.planner} referenced by task {task_id} no longer exists'}

        project = self.db.get(Project, task.project) if task.project else None
        repo_root = os.path.abspath(project.repo_root) if project and project.repo_root else None
        if not repo_root:
            return {'error': 'Plan criticism requires a configured project repo_root.'}

        critic_run = plan_executor.create_critic_run(
            self.db, task, repo_root, critic_agent=critic_agent,
        )

        deadline = time.monotonic() + _GENERATE_SPEC_PLAN_WAIT_SECONDS
        while True:
            self.db.rollback()
            run = self.db.get(AgentRun, critic_run.id)
            if run is None:
                return {'error': f'AgentRun {critic_run.id} not found'}
            if run.status == 'failed':
                return {'error': run.error_message or 'Plan critic failed', 'task_id': task_id}
            if run.status == 'success':
                task = self.db.get(Task, task_id)
                return {
                    'action': (
                        'spec_plan_critic_rejected'
                        if task.plan_critic_status == 'reject'
                        else 'spec_plan_critiqued'
                    ),
                    'task_id': task_id,
                    'planner': task.planner,
                    'plan_critic': task.plan_critic,
                    'plan_critic_status': task.plan_critic_status,
                    'plan_critic_findings': task.plan_critic_findings,
                    'awaiting_approval': bool(task.awaiting_approval),
                    'approval_prompt': task.approval_prompt,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._run_handle(run, task_id)
            await asyncio.sleep(min(1.0, remaining))

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
