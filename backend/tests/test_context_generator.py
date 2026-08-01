"""Tests for ContextChecker, get_matching_rules, and save_project_context MCP tool."""

from __future__ import annotations

import json

import pytest
from app.db.models import Project, ProjectRule, Task, Agent
from app.services.command_router import CommandRouter
from app.services.context_generator import ContextChecker, get_matching_rules
from app.services.task_orchestration import OrchestrationError, TaskOrchestrationService


class TestGetMatchingRules:
    def test_no_files_returns_all_rules(self, db_session):
        project = Project(id="proj-1", name="Test")
        rule1 = ProjectRule(
            id="r1", project_id="proj-1", name="rule1", globs=["*.py"], content="rule 1 content", priority=10
        )
        rule2 = ProjectRule(
            id="r2", project_id="proj-1", name="rule2", globs=["*.ts"], content="rule 2 content", priority=5
        )
        db_session.add_all([project, rule1, rule2])
        db_session.flush()

        result = get_matching_rules(db_session, "proj-1", None)

        assert len(result) == 2
        assert result[0].name == "rule1"  # Higher priority first

    def test_matches_glob_pattern(self, db_session):
        project = Project(id="proj-2", name="Test Glob")
        rule_py = ProjectRule(
            id="r-py", project_id="proj-2", name="python", globs=["*/**/*.py", "*.py"], content="py rule"
        )
        rule_ts = ProjectRule(
            id="r-ts", project_id="proj-2", name="typescript", globs=["*/**/*.ts", "*.ts"], content="ts rule"
        )
        db_session.add_all([project, rule_py, rule_ts])
        db_session.flush()

        result = get_matching_rules(db_session, "proj-2", ["backend/app/main.py"])

        assert len(result) == 1
        assert result[0].name == "python"

    def test_multiple_files_match_multiple_rules(self, db_session):
        project = Project(id="proj-3", name="Test Multi")
        rule_py = ProjectRule(
            id="r-py", project_id="proj-3", name="python", globs=["*/**/*.py", "*.py"], content="py rule"
        )
        rule_ts = ProjectRule(
            id="r-ts", project_id="proj-3", name="typescript", globs=["*/**/*.ts", "*.ts"], content="ts rule"
        )
        db_session.add_all([project, rule_py, rule_ts])
        db_session.flush()

        result = get_matching_rules(
            db_session, "proj-3", ["backend/app/main.py", "frontend/app.ts"]
        )

        assert len(result) == 2

    def test_double_star_matches_direct_children(self, db_session):
        project = Project(id="proj-ds", name="DS")
        rule = ProjectRule(
            id="r-ds", project_id="proj-ds", name="services",
            globs=["backend/app/services/**/*.py"], content="services rule",
        )
        db_session.add_all([project, rule])
        db_session.flush()

        direct = get_matching_rules(
            db_session, "proj-ds", ["backend/app/services/outbox.py"]
        )
        nested = get_matching_rules(
            db_session, "proj-ds", ["backend/app/services/providers/base.py"]
        )
        assert [r.name for r in direct] == ["services"]
        assert [r.name for r in nested] == ["services"]


class TestContextChecker:
    def test_check_project_ready_missing_context(self, db_session):
        project = Project(id="proj-check-1", name="Check 1", context_md=None, context_generated=False)
        db_session.add(project)
        db_session.flush()

        checker = ContextChecker(db_session)
        result = checker.check_project_ready("proj-check-1")

        assert result["exists"] is True
        assert result["ready"] is False
        assert result["has_context"] is False

    def test_check_project_ready_complete(self, db_session):
        project = Project(id="proj-check-2", name="Check 2", context_md="# Context", context_generated=True)
        rule = ProjectRule(id="r1", project_id="proj-check-2", name="rule1", globs=[], content="rule content")
        db_session.add_all([project, rule])
        db_session.flush()

        checker = ContextChecker(db_session)
        result = checker.check_project_ready("proj-check-2")

        assert result["exists"] is True
        assert result["ready"] is True
        assert result["has_context"] is True
        assert result["has_rules"] is True


class TestSaveProjectContext:
    async def _call(self, router, project_id, context_md, rules=None, task_id="task-save-1"):
        args = json.dumps({
            "task_id": task_id,
            "project_id": project_id,
            "context_md": context_md,
            "rules": rules or [],
        })
        return await router.execute("save_project_context", args, "session-1")

    @staticmethod
    def _seed_task(db_session, project_id, task_id="task-save-1"):
        # The handler refuses a cross-project write, so the scoping task must
        # exist and belong to the target project.
        db_session.add(Task(
            id=task_id, title="ctx gen", project=project_id,
            status="dispatched", legacy_no_ac=True,
        ))
        db_session.flush()

    @pytest.mark.asyncio
    async def test_save_success(self, db_session):
        project = Project(id="proj-save-1", name="Save Test")
        db_session.add(project)
        db_session.flush()
        self._seed_task(db_session, "proj-save-1")

        router = CommandRouter(db_session)
        result = await self._call(
            router,
            "proj-save-1",
            "# Context\nSome content",
            rules=[{"name": "rule1", "globs": ["*.py"], "content": "content 1"}],
        )

        assert result["status"] == "success"
        assert result["task_id"] == "task-save-1"
        assert result["project_id"] == "proj-save-1"
        assert result["context_lines"] == 2
        assert result["rules_count"] == 1

        db_session.refresh(project)
        assert project.context_md == "# Context\nSome content"
        assert project.context_generated is True
        rules = db_session.query(ProjectRule).filter_by(project_id="proj-save-1").all()
        assert len(rules) == 1
        assert rules[0].name == "rule1"

    @pytest.mark.asyncio
    async def test_save_rejects_context_over_150_lines(self, db_session):
        project = Project(id="proj-save-2", name="Too Long")
        db_session.add(project)
        db_session.flush()
        self._seed_task(db_session, "proj-save-2")

        router = CommandRouter(db_session)
        too_long = "\n".join(f"line {i}" for i in range(151))
        result = await self._call(router, "proj-save-2", too_long)

        assert "error" in result
        db_session.refresh(project)
        assert project.context_md is None
        assert project.context_generated is False

    @pytest.mark.asyncio
    async def test_save_replaces_existing_rules(self, db_session):
        project = Project(id="proj-save-3", name="Replace Test")
        db_session.add(project)
        db_session.flush()
        self._seed_task(db_session, "proj-save-3")

        router = CommandRouter(db_session)
        await self._call(
            router,
            "proj-save-3",
            "# Context v1",
            rules=[{"name": "old-rule", "globs": [], "content": "old content"}],
        )

        await self._call(
            router,
            "proj-save-3",
            "# Context v2",
            rules=[{"name": "new-rule", "globs": [], "content": "new content"}],
        )

        rules = db_session.query(ProjectRule).filter_by(project_id="proj-save-3").all()
        assert len(rules) == 1
        assert rules[0].name == "new-rule"
        names = [r.name for r in rules]
        assert "old-rule" not in names

    @pytest.mark.asyncio
    async def test_save_rejects_duplicate_rule_names(self, db_session):
        project = Project(id="proj-save-4", name="Dup Test")
        db_session.add(project)
        db_session.flush()
        self._seed_task(db_session, "proj-save-4")

        router = CommandRouter(db_session)
        result = await self._call(
            router,
            "proj-save-4",
            "# Context",
            rules=[
                {"name": "dup-rule", "globs": [], "content": "content a"},
                {"name": "dup-rule", "globs": [], "content": "content b"},
            ],
        )

        assert "error" in result
        assert "dup-rule" in result["error"]

        db_session.refresh(project)
        assert project.context_md is None
        assert project.context_generated is False
        rules = db_session.query(ProjectRule).filter_by(project_id="proj-save-4").all()
        assert len(rules) == 0







    @pytest.mark.asyncio
    async def test_save_rejects_cross_project_write(self, db_session):
        db_session.add_all([
            Project(id="proj-save-5", name="Mine"),
            Project(id="proj-save-6", name="Theirs"),
        ])
        db_session.flush()
        self._seed_task(db_session, "proj-save-5")

        router = CommandRouter(db_session)
        result = await self._call(router, "proj-save-6", "# Context")

        assert "error" in result
        assert "cross-project" in result["error"]
        victim = db_session.get(Project, "proj-save-6")
        assert victim.context_md is None

    @pytest.mark.asyncio
    async def test_save_rejects_non_string_glob(self, db_session):
        project = Project(id="proj-save-7", name="Bad Glob")
        db_session.add(project)
        db_session.flush()
        self._seed_task(db_session, "proj-save-7")

        router = CommandRouter(db_session)
        result = await self._call(
            router,
            "proj-save-7",
            "# Context",
            rules=[{"name": "bad", "globs": [123], "content": "x"}],
        )

        assert "error" in result
        assert "list of strings" in result["error"]
