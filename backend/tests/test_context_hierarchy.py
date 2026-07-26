import pytest
import os
from app.db.models import Project, Session as SessionModel, Task, LLMUsage
from app.services.context_hierarchy import ContextHierarchy
from app.services.coordinator import CoordinatorService
from app.services.command_router import CommandRouter
from app.services.providers import ProviderResponse
from app.services.llm_client import UsageCounts


def test_global_context_loaded_and_cached(db_session):
    hierarchy = ContextHierarchy(db_session)
    global_ctx1 = hierarchy.get_global_context()
    global_ctx2 = hierarchy.get_global_context()

    assert len(global_ctx1) > 0
    assert global_ctx1[0]["role"] == "system"
    assert "Control Tower V2" in global_ctx1[0]["content"]
    assert global_ctx1 == global_ctx2


def test_project_context_from_db(db_session):
    project = Project(
        id="proj-test-1",
        name="Test Project 1",
        description="Project description 1",
        context_md="Detailed context markdown",
    )
    db_session.add(project)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    proj_ctx = hierarchy.get_project_context("proj-test-1")

    assert len(proj_ctx) == 1
    assert proj_ctx[0]["role"] == "user"
    assert "[Project Context: Test Project 1]" in proj_ctx[0]["content"]
    assert "Project description 1" in proj_ctx[0]["content"]
    assert "Detailed context markdown" in proj_ctx[0]["content"]


def test_project_context_from_file_fallback(db_session, tmp_path, monkeypatch):
    project = Project(
        id="proj-file-test",
        name="File Test Project",
        description="File description",
    )
    db_session.add(project)
    db_session.commit()

    proj_dir = tmp_path / "projects" / "proj-file-test"
    proj_dir.mkdir(parents=True)
    context_file = proj_dir / "context.md"
    context_file.write_text("File based context info", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    hierarchy = ContextHierarchy(db_session)
    proj_ctx = hierarchy.get_project_context("proj-file-test")

    assert len(proj_ctx) == 1
    assert "File based context info" in proj_ctx[0]["content"]


def test_build_messages_tiered_ordering_and_cache_control(db_session):
    project = Project(
        id="proj-tiered",
        name="Tiered Project",
        description="Tiered description",
    )
    task = Task(
        id="TASK-001",
        project="proj-tiered",
        title="Tiered Task",
        status="todo",
    )
    session = SessionModel(
        id="sess-tiered",
        task_id="TASK-001",
        messages=[
            {"id": "msg-1", "role": "user", "content": "Hello", "status": "complete"},
            {"id": "msg-2", "role": "assistant", "content": "Hi", "status": "complete"},
        ],
    )
    db_session.add_all([project, task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    messages = hierarchy.build_messages(session)

    # 1. Global Context (System)
    assert messages[0]["role"] == "system"
    assert messages[0].get("cache_control") == {"type": "ephemeral"}

    # 2. Project Context (User with Project info)
    assert messages[1]["role"] == "user"
    assert "[Project Context: Tiered Project]" in messages[1]["content"]
    assert messages[1].get("cache_control") == {"type": "ephemeral"}

    # 3. Task Context (Task System Header + Session messages)
    assert messages[2]["role"] == "system"
    assert "Task [TASK-001]" in messages[2]["content"]
    assert "cache_control" not in messages[2]

    assert messages[3]["role"] == "user"
    assert messages[3]["content"] == "Hello"
    assert messages[4]["role"] == "assistant"
    assert messages[4]["content"] == "Hi"


def test_context_compaction(db_session):
    session = SessionModel(
        id="sess-compact",
        messages=[
            {"id": f"msg-{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}", "status": "complete"}
            for i in range(60)
        ],
    )
    db_session.add(session)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    assert len(session.messages) == 60

    compacted = hierarchy.compact_context(session, threshold=50)
    assert compacted is True
    assert len(session.messages) == 11  # 1 summary msg + 10 kept
    assert session.messages[0]["role"] == "system"
    assert "Context Compaction" in session.messages[0]["content"]


@pytest.mark.asyncio
async def test_compact_command_via_router(db_session):
    session = SessionModel(
        id="sess-cmd-compact",
        messages=[
            {"id": f"msg-{i}", "role": "user", "content": f"Message {i}", "status": "complete"}
            for i in range(15)
        ],
    )
    db_session.add(session)
    db_session.commit()

    router = CommandRouter(db_session)
    cmd, args = router.parse("/compact")
    assert cmd == "compact_context"

    result = await router.execute(cmd, args, session.id)
    assert result["action"] == "compacted"
    assert result["compacted"] is True

    db_session.refresh(session)
    assert len(session.messages) == 11
    assert "Context Compaction" in session.messages[0]["content"]
