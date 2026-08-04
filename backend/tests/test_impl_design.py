import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.db.models import Agent, ImplDesign, Project, Task
from app.services.command_router import CommandRouter
from app.services.impl_design import save_design
from app.services.task_orchestration import PrerequisiteError, TaskOrchestrationService


def _head(repo_root: str) -> str:
    return subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _seed(db_session, git_repo_root: str, task_id: str = "ID-001") -> tuple[Task, str]:
    module = Path(git_repo_root) / "app.py"
    module.write_text("def run_task():\n    return True\n")
    subprocess.run(["git", "-C", git_repo_root, "add", "app.py"], check=True)
    subprocess.run(
        ["git", "-C", git_repo_root, "commit", "-q", "-m", "add app"], check=True
    )
    project = Project(id="impl-project", name="Implementation project", repo_root=git_repo_root)
    task = Task(
        id=task_id,
        project=project.id,
        title="Implementation design task",
        acceptance_criteria=["The task works"],
        mode="bypass",
    )
    db_session.add_all([project, task])
    db_session.commit()
    return task, _head(git_repo_root)


def _payload(head: str, **overrides) -> dict:
    payload = {
        "action": "create",
        "summary": "Implement the task through the existing service boundary.",
        "files": [{"path": "app.py", "action": "modify", "why": "reuse entry point"}],
        "changes": [{
            "symbol": "run_task",
            "signature": "run_task() -> bool",
            "behavior": "Return the task result using the existing contract.",
            "edge_cases": ["missing input"],
        }],
        "data_changes": [],
        "test_plan": [{"case": "run_task returns true"}],
        "risks": [],
        "non_goals": ["Do not redesign the task FSM."],
        "derived_from_sha": head,
        "authored_by": "strong-model",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_valid_design_passes_all_six_mechanical_checks(db_session, git_repo_root):
    task, head = _seed(db_session, git_repo_root)
    with patch(
        "app.services.impl_design.symbol_exists",
        new=AsyncMock(return_value=True),
    ):
        result = await save_design(db_session, task.id, _payload(head))

    completeness = result["completeness"]
    assert completeness["passed"] is True
    assert len(completeness["checks"]) == 6
    assert all(check["passed"] for check in completeness["checks"])
    assert db_session.query(ImplDesign).filter_by(task_id=task.id).one()


@pytest.mark.asyncio
async def test_design_with_ghost_file_fails_with_file_name(db_session, git_repo_root):
    task, head = _seed(db_session, git_repo_root)
    with patch(
        "app.services.impl_design.symbol_exists",
        new=AsyncMock(return_value=True),
    ):
        result = await save_design(
            db_session,
            task.id,
            _payload(head, files=[{"path": "ghost.py", "action": "modify", "why": "no such file"}]),
        )

    check = next(item for item in result["completeness"]["checks"] if item["name"] == "file_paths")
    assert check["passed"] is False
    assert "ghost.py" in check["reason"]


@pytest.mark.asyncio
async def test_design_with_ghost_symbol_fails_with_symbol_name(db_session, git_repo_root):
    task, head = _seed(db_session, git_repo_root)
    with patch(
        "app.services.impl_design.symbol_exists",
        new=AsyncMock(return_value=False),
    ):
        result = await save_design(
            db_session,
            task.id,
            _payload(head, changes=[{"symbol": "ghost_symbol", "behavior": "Never found."}]),
        )

    check = next(item for item in result["completeness"]["checks"] if item["name"] == "symbols")
    assert check["passed"] is False
    assert "ghost_symbol" in check["reason"]


@pytest.mark.asyncio
async def test_design_without_test_plan_fails(db_session, git_repo_root):
    task, head = _seed(db_session, git_repo_root)
    with patch(
        "app.services.impl_design.symbol_exists",
        new=AsyncMock(return_value=True),
    ):
        result = await save_design(db_session, task.id, _payload(head, test_plan=[]))

    check = next(item for item in result["completeness"]["checks"] if item["name"] == "test_plan")
    assert check["passed"] is False
    assert check["reason"] == "test_plan is empty"


@pytest.mark.asyncio
async def test_impl_design_is_available_through_mcp_router(db_session, git_repo_root):
    task, head = _seed(db_session, git_repo_root, task_id="ID-TOOL")
    with patch(
        "app.services.impl_design.symbol_exists",
        new=AsyncMock(return_value=True),
    ):
        result = await CommandRouter(db_session).execute_tool(
            "impl_design", _payload(head) | {"task_id": task.id}, "session-1"
        )
    assert result["action"] == "impl_design_saved"
    assert result["design"]["completeness"]["passed"] is True


def test_incomplete_design_blocks_cheap_executor_but_no_design_is_legacy_compatible(db_session):
    db_session.add(Project(id="dispatch-project", name="Dispatch project", repo_root="/tmp"))
    db_session.add(Agent(id="@cheap", name="Cheap", role="executor", cli="codex", effort="low"))
    db_session.add(Agent(id="@strong", name="Strong", role="executor", cli="codex", effort="high"))
    no_design = Task(
        id="ID-NONE", project="dispatch-project", title="Legacy task",
        acceptance_criteria=["works"], mode="bypass",
    )
    incomplete = Task(
        id="ID-INCOMPLETE", project="dispatch-project", title="Designed task",
        acceptance_criteria=["works"], mode="bypass",
    )
    db_session.add_all([no_design, incomplete])
    db_session.flush()
    db_session.add(ImplDesign(task_id=incomplete.id, completeness={"passed": False, "checks": []}))
    db_session.commit()

    service = TaskOrchestrationService(db_session)
    with patch("app.services.task_orchestration.build_dispatch_command", return_value=("codex", "/tmp", "codex")):
        legacy = service.request_dispatch(
            task_id=no_design.id, agent_id="@cheap", actor="@operator", idempotency_key="legacy"
        )
    assert legacy.task.status == "dispatched"

    with pytest.raises(PrerequisiteError, match="impl_design"):
        service.request_dispatch(
            task_id=incomplete.id, agent_id="@cheap", actor="@operator", idempotency_key="blocked"
        )
