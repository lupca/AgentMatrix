import pytest
import os
from app.db.models import Agent, Project, Session as SessionModel, Task, LLMUsage
from app.services.context_hierarchy import ContextHierarchy, PROJECT_CONTEXT_MAX_CHARS
from app.services.coordinator import CoordinatorService
from app.services.command_router import CommandRouter
from app.services.providers import ProviderResponse
from app.services.llm_client import UsageCounts
from app.graph.context import build_context_snapshot, invalidate_context_snapshot


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


def test_build_messages_tiered_ordering_and_pinned_flag(db_session):
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
        project_id="proj-tiered",
        context_level="task",
        messages=[
            {"id": "msg-1", "role": "user", "content": "Hello", "status": "complete"},
            {"id": "msg-2", "role": "assistant", "content": "Hi", "status": "complete"},
        ],
    )
    db_session.add_all([project, task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    messages = hierarchy.build_messages(session)

    # 1. Global Context (System, pinned)
    assert messages[0]["role"] == "system"
    assert messages[0].get("pinned") is True
    assert "cache_control" not in messages[0]

    # 2. Project Context (User with Project info, pinned)
    assert messages[1]["role"] == "user"
    assert "[Project Context: Tiered Project]" in messages[1]["content"]
    assert messages[1].get("pinned") is True
    assert "cache_control" not in messages[1]

    # 3. Context snapshot (own dynamic message, not pinned)
    assert messages[2]["role"] == "system"
    assert "## System State" in messages[2]["content"]
    assert "pinned" not in messages[2]
    assert "cache_control" not in messages[2]

    # 4. Task Context (Task System Header + Session messages)
    assert messages[3]["role"] == "system"
    assert "Task [TASK-001]" in messages[3]["content"]
    assert "pinned" not in messages[3]
    assert "cache_control" not in messages[3]

    assert messages[4]["role"] == "user"
    assert messages[4]["content"] == "Hello"
    assert messages[5]["role"] == "assistant"
    assert messages[5]["content"] == "Hi"


def test_build_messages_prefix_stable_across_task_mutation(db_session):
    """Global + Project message bytes stay identical across a task mutation;
    only the snapshot message (Tier 2.5) changes."""
    project = Project(
        id="proj-stable",
        name="Stable Project",
        description="Stable description",
    )
    task = Task(
        id="TASK-STABLE-1",
        project="proj-stable",
        title="Stable Task",
        status="todo",
    )
    session = SessionModel(
        id="sess-stable",
        task_id="TASK-STABLE-1",
        project_id="proj-stable",
        context_level="task",
        messages=[],
    )
    db_session.add_all([project, task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    before = hierarchy.build_messages(session)

    # Mutate: add a new task in the same project, which changes the snapshot's
    # task-count/recent-tasks content.
    new_task = Task(
        id="TASK-STABLE-2",
        project="proj-stable",
        title="Second Stable Task",
        status="todo",
    )
    db_session.add(new_task)
    db_session.commit()
    invalidate_context_snapshot(db_session, project_id="proj-stable")

    after = hierarchy.build_messages(session)

    assert before[0] == after[0]  # Global tier bytes unchanged
    assert before[1] == after[1]  # Project tier bytes unchanged
    assert before[2] != after[2]  # Snapshot message changed
    assert "TASK-STABLE-2" in after[2]["content"]
    assert "TASK-STABLE-2" not in before[2]["content"]


def test_system_state_snapshot_stays_within_cap_at_scale(db_session):
    """20 projects + 50 agents must not blow the ~30 line / ~600 token cap:
    enumeration is top-N, the rest is counted only (ADR-001 §D2)."""
    for i in range(20):
        db_session.add(Project(id=f"proj-{i:02d}", name=f"Project {i:02d}", status="active"))
    for i in range(50):
        db_session.add(
            Agent(
                id=f"@agent-{i:02d}",
                name=f"Agent {i:02d}",
                role="executor",
                agent_type="api" if i % 2 == 0 else "cli",
                cli="codex",
            )
        )
    session = SessionModel(id="sess-scale", messages=[])
    db_session.add(session)
    db_session.commit()

    snapshot = build_context_snapshot(session, db_session)
    lines = snapshot.splitlines()

    assert len(lines) <= 30
    assert len(snapshot) <= 2_400  # ~600 tokens at ~4 chars/token
    assert "- Projects: 20 active" in snapshot
    assert "+12 more" in snapshot  # top 8 enumerated, remainder counted
    assert "- Agents: 50 configured (25 api / 25 cli; default: none)" in snapshot


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


def test_get_tool_definitions_marks_rare_tools_deferred(db_session):
    hierarchy = ContextHierarchy(db_session)
    tools = hierarchy.get_tool_definitions()

    names_eager = {t["name"] for t in tools if not t.get("defer_loading")}
    names_deferred = {t["name"] for t in tools if t.get("defer_loading")}

    assert "create_task" in names_eager
    assert "get_status" in names_eager
    assert "dispatch_task" in names_deferred
    assert "compact_context" in names_deferred

    # The tool-search tool itself must never be deferred, and must be present
    # whenever any tool is deferred.
    search_tools = [t for t in tools if t["name"] == "tool_search_tool_regex"]
    assert len(search_tools) == 1
    assert not search_tools[0].get("defer_loading")


def test_project_context_auto_memory_from_recent_tasks(db_session):
    project = Project(id="proj-memory", name="Memory Project", description="Desc")
    tasks = [
        Task(
            id=f"MEM-{i}",
            project="proj-memory",
            title=f"Completed task {i}",
            status="done",
            verdict="pass",
            executor="@claude",
            reviewer="@opus",
            result_ref=f"ref-{i}",
        )
        for i in range(3)
    ]
    db_session.add(project)
    db_session.add_all(tasks)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    proj_ctx = hierarchy.get_project_context("proj-memory")

    assert len(proj_ctx) == 1
    content = proj_ctx[0]["content"]
    assert "[Project Memory: recent completed tasks]" in content
    assert "MEM-0" in content
    assert "verdict: pass" in content


def test_project_context_capped_at_25kb(db_session):
    project = Project(
        id="proj-huge",
        name="Huge Project",
        description="x" * 40_000,
    )
    db_session.add(project)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    proj_ctx = hierarchy.get_project_context("proj-huge")

    assert len(proj_ctx) == 1
    assert len(proj_ctx[0]["content"]) <= PROJECT_CONTEXT_MAX_CHARS
    assert "truncated" in proj_ctx[0]["content"]


def test_task_context_enriches_with_langgraph_state(db_session):
    from app.graph.builder import build_graph
    from langgraph.checkpoint.memory import MemorySaver

    task = Task(id="GRAPH-1", project="proj-graph", title="Graph Task", status="dispatched")
    session = SessionModel(
        id="sess-graph",
        task_id="GRAPH-1",
        project_id="proj-graph",
        context_level="task",
        messages=[],
    )
    db_session.add_all([task, session])
    db_session.commit()

    graph = build_graph(checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "GRAPH-1"}}
    graph.invoke({"raw_input": "ship it", "task_id": "GRAPH-1"}, config=config)

    hierarchy = ContextHierarchy(db_session, graph=graph)
    task_ctx = hierarchy.get_task_context(session)

    assert len(task_ctx) == 1
    assert "[LangGraph State]" in task_ctx[0]["content"]
    assert "verdict=pass" in task_ctx[0]["content"]


def test_task_context_without_graph_is_unaffected(db_session):
    task = Task(id="NOGRAPH-1", project="proj-nograph", title="No Graph Task", status="todo")
    session = SessionModel(
        id="sess-nograph",
        task_id="NOGRAPH-1",
        project_id="proj-nograph",
        context_level="task",
        messages=[],
    )
    db_session.add_all([task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    task_ctx = hierarchy.get_task_context(session)

    assert len(task_ctx) == 1
    assert "[LangGraph State]" not in task_ctx[0]["content"]


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
