"""Integration tests for dispatch with project context and rules injection."""

from __future__ import annotations

import pytest
from app.db.models import Agent, Project, ProjectRule, Task
from app.services.command_builder import build_dispatch_command
from app.services.task_orchestration import PrerequisiteError, TaskOrchestrationService


class TestDispatchWithContext:
    def test_dispatch_without_context_supervised_mode_raises(self, db_session):
        project = Project(
            id="proj-no-ctx",
            name="No Context Project",
            repo_root="/tmp",
            context_md=None,
            context_generated=False,
            autonomy_policy={"autonomy": "supervised"},
        )
        task = Task(
            id="task-1",
            project="proj-no-ctx",
            title="Test task",
            status="todo",
            legacy_no_ac=True,
        )
        agent = Agent(id="agent-1", name="Executor Agent", role="executor", model="claude-sonnet")
        db_session.add_all([project, task, agent])
        db_session.flush()

        service = TaskOrchestrationService(db_session)
        with pytest.raises(PrerequisiteError) as excinfo:
            service.request_dispatch(
                task_id="task-1",
                agent_id="agent-1",
                actor="test",
                idempotency_key="key-1",
            )
        assert "missing context" in str(excinfo.value)

    def test_dispatch_without_context_bypass_mode_triggers_gen(self, db_session):
        project = Project(
            id="proj-auto-gen",
            name="Auto Gen Project",
            repo_root="/tmp",
            context_md=None,
            context_generated=False,
            autonomy_policy={"autonomy": "bypass"},
        )
        task = Task(
            id="task-2",
            project="proj-auto-gen",
            title="Test task auto",
            status="todo",
            legacy_no_ac=True,
        )
        agent = Agent(id="agent-1", name="Executor Agent", role="executor", model="claude-sonnet")
        db_session.add_all([project, task, agent])
        db_session.flush()

        service = TaskOrchestrationService(db_session)
        result = service.request_dispatch(
            task_id="task-2",
            agent_id="agent-1",
            actor="test",
            idempotency_key="key-2",
        )
        assert result.context["action"] == "context_gen_triggered"
        assert result.context["context_task_id"].startswith("ctx-")  # Hash-based ID

    def test_build_dispatch_command_injects_context_and_matching_rules(self, db_session):
        project = Project(
            id="proj-full-ctx",
            name="Full Context Project",
            repo_root="/tmp",
            context_md="# Stack\nFastAPI + Postgres",
        )
        rule_all = ProjectRule(
            id="r-all",
            project_id="proj-full-ctx",
            name="Global Boundary",
            globs=[],
            content="Never break production",
            priority=10,
        )
        rule_py = ProjectRule(
            id="r-py",
            project_id="proj-full-ctx",
            name="Python Rule",
            globs=["**/*.py"],
            content="Use type hints",
            priority=5,
        )
        task = Task(
            id="task-3",
            project="proj-full-ctx",
            title="Update API",
            status="todo",
            legacy_no_ac=True,
            files=["app/main.py"],
        )
        agent = Agent(id="agent-1", name="Executor Agent", role="executor", model="claude-sonnet")
        db_session.add_all([project, rule_all, rule_py, task, agent])
        db_session.flush()

        command, repo_root, cli = build_dispatch_command(
            task, agent, project, db=db_session
        )

        assert "[Project Context]" in command
        assert "# Stack\nFastAPI + Postgres" in command
        assert "[Project Rules]" in command
        assert "Global Boundary" in command
        assert "Python Rule" in command

    def test_context_status_endpoint(self, client, db_session):
        project = Project(
            id="proj-status-test",
            name="Status Project",
            context_md="# Context",
            context_generated=True,
        )
        rule = ProjectRule(
            id="r-status",
            project_id="proj-status-test",
            name="Rule",
            globs=[],
            content="Content",
        )
        db_session.add_all([project, rule])
        db_session.commit()

        res = client.get("/api/projects/proj-status-test/context-status")
        assert res.status_code == 200
        data = res.json()
        assert data["ready"] is True
        assert data["has_context"] is True
        assert data["has_rules"] is True
