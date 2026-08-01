"""Tests for ContextChecker, get_matching_rules, and save_project_context MCP tool."""

from __future__ import annotations

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


class TestSaveProjectContextMCP:
    @pytest.mark.asyncio
    async def test_save_project_context_tool(self, db_session):
        project = Project(id="proj-mcp", name="MCP Test", repo_root="/tmp/test")
        db_session.add(project)
        db_session.flush()

        router = CommandRouter(db_session)
        result = await router.execute_tool(
            "save_project_context",
            {
                "project_id": "proj-mcp",
                "context_md": "# Test Project\n## Stack\nPython + FastAPI",
                "rules": [
                    {
                        "name": "api",
                        "globs": ["app/api/**/*.py"],
                        "content": "Use FastAPI routers",
                    }
                ],
            },
            session_id="test-mcp-session",
        )

        assert result["status"] == "saved"
        assert result["rules_count"] == 1

        db_project = db_session.get(Project, "proj-mcp")
        assert "Test Project" in db_project.context_md
        assert db_project.context_generated is True

        rules = db_session.query(ProjectRule).filter_by(project_id="proj-mcp").all()
        assert len(rules) == 1
        assert rules[0].name == "api"


class TestProjectRulesAPI:
    def test_rules_crud(self, client, db_session):
        project = Project(id="proj-api", name="API Project")
        db_session.add(project)
        db_session.commit()

        # List (empty)
        res = client.get("/api/projects/proj-api/rules")
        assert res.status_code == 200
        assert res.json() == []

        # Create
        res = client.post(
            "/api/projects/proj-api/rules",
            json={
                "name": "architecture",
                "description": "Arch rule",
                "globs": ["backend/**/*.py"],
                "content": "Keep layers clean",
                "priority": 10,
            },
        )
        assert res.status_code == 201
        data = res.json()
        rule_id = data["id"]
        assert data["name"] == "architecture"

        # Update
        res = client.put(
            f"/api/projects/proj-api/rules/{rule_id}",
            json={"priority": 20},
        )
        assert res.status_code == 200
        assert res.json()["priority"] == 20

        # Delete
        res = client.delete(f"/api/projects/proj-api/rules/{rule_id}")
        assert res.status_code == 204
