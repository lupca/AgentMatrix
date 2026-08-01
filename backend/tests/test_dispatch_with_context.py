"""Integration tests for dispatch with project context and rules injection."""

from __future__ import annotations

import pytest
from app.db.models import Agent, Project, ProjectRule, Task
from app.services.command_builder import build_dispatch_command
from app.services.task_orchestration import PrerequisiteError, TaskOrchestrationService


class TestDispatchWithContext:


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

        command, repo_root, cli = build_dispatch_command(task, agent, project)

        assert cli == "claude"
        assert repo_root == "/tmp"
        assert "Execute task task-3: Update API" in command
