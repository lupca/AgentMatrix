import hashlib
import json
import pytest
import os
from app.db.models import Agent, Project, Session as SessionModel, Task, LLMUsage
from app.services.context_hierarchy import ContextHierarchy, PROJECT_CONTEXT_MAX_CHARS
from app.services.coordinator import CoordinatorService
from app.services.command_router import CommandRouter
from app.services.providers import ProviderResponse
from app.services.providers.openai_adapter import OpenAIAdapter
from app.services.llm_client import UsageCounts
from app.graph.context import build_context_snapshot, invalidate_context_snapshot


def _tool_turn(turn_id: str, *, call_id: str, name: str, content: str, final_text: str) -> list[dict]:
    """A realistic turn: user ask -> assistant tool_calls -> tool result -> assistant reply."""
    return [
        {"role": "user", "content": f"Please run {name}", "status": "complete", "turn_id": turn_id},
        {
            "role": "assistant",
            "content": "",
            "status": "complete",
            "turn_id": turn_id,
            "tool_calls": [{"id": call_id, "name": name, "input": {}}],
        },
        {
            "role": "tool",
            "name": name,
            "tool_call_id": call_id,
            "turn_id": turn_id,
            "status": "complete",
            "content": content,
        },
        {"role": "assistant", "content": final_text, "status": "complete", "turn_id": turn_id},
    ]


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

    # Append-only history stays before the dynamic snapshot.
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == "Hello"
    assert messages[3]["role"] == "assistant"
    assert messages[3]["content"] == "Hi"

    # Snapshot is followed by the live task suffix.
    assert messages[4]["role"] == "system"
    assert "## System State" in messages[4]["content"]
    assert "pinned" not in messages[4]
    assert messages[5]["role"] == "system"
    assert "Task [TASK-001]" in messages[5]["content"]


def test_old_tool_results_are_pruned_but_decision_fields_survive(db_session):
    project = Project(id="proj-prune", name="Prune Project")
    session = SessionModel(
        id="sess-prune", project_id="proj-prune", context_level="project",
        messages=[
            {"role": "tool", "name": "update_task", "tool_call_id": f"call-{i}",
             "turn_id": f"turn-{i}",
             "content": '{"task_id": "CTV2-095", "verdict": "pass", "constraints": ["four-eyes"]}',
             "status": "complete"}
            for i in range(4)
        ],
    )
    db_session.add_all([project, session])
    db_session.commit()
    hierarchy = ContextHierarchy(db_session)
    hierarchy.tool_result_replay_turns = 2

    tools = [m for m in hierarchy.get_task_context(session) if m["role"] == "tool"]
    assert len(tools) == 4
    assert "[Pruned tool result]" in tools[0]["content"]
    assert "CTV2-095" in tools[0]["content"]
    assert '"verdict": "pass"' in tools[0]["content"]
    assert tools[-1]["content"].startswith('{"task_id"')


def test_zero_replay_turns_prunes_every_tool_message(db_session):
    """TOOL_RESULT_REPLAY_TURNS = 0 means keep the 0 most recent turns full,
    i.e. prune all of them — not tool_turns[-0:], which is the whole list
    and would keep everything instead."""
    project = Project(id="proj-prune-zero", name="Prune Zero Project")
    session = SessionModel(
        id="sess-prune-zero", project_id="proj-prune-zero", context_level="project",
        messages=[
            {"role": "tool", "name": "update_task", "tool_call_id": f"call-{i}",
             "turn_id": f"turn-{i}",
             "content": '{"task_id": "CTV2-095", "verdict": "pass"}',
             "status": "complete"}
            for i in range(20)
        ],
    )
    db_session.add_all([project, session])
    db_session.commit()
    hierarchy = ContextHierarchy(db_session)
    hierarchy.tool_result_replay_turns = 0

    tools = [m for m in hierarchy.get_task_context(session) if m["role"] == "tool"]
    assert len(tools) == 20
    assert all(t["content"].startswith("[Pruned tool result]") for t in tools)


def test_pruned_multi_tool_call_turn_keeps_one_summary_per_tool_message(db_session):
    """A turn with two tool calls must survive pruning as two summaries, each
    carrying its own tool_call_id/name — not collapsed into one per turn_id
    (which would silently drop the second call's decision fields and orphan
    the first tool_call_id the assistant message referenced)."""
    project = Project(id="proj-multi", name="Multi Project")
    old_turn = [
        {
            "role": "assistant",
            "content": "",
            "status": "complete",
            "turn_id": "turn-old",
            "tool_calls": [
                {"id": "call-old-1", "name": "update_task", "input": {}},
                {"id": "call-old-2", "name": "record_verdict", "input": {}},
            ],
        },
        {
            "role": "tool",
            "name": "update_task",
            "tool_call_id": "call-old-1",
            "turn_id": "turn-old",
            "status": "complete",
            "content": json.dumps({"task_id": "CTV2-095", "verdict": "pass"}),
        },
        {
            "role": "tool",
            "name": "record_verdict",
            "tool_call_id": "call-old-2",
            "turn_id": "turn-old",
            "status": "complete",
            "content": json.dumps({"task_id": "CTV2-200", "verdict": "changes_requested"}),
        },
    ]
    recent_turns = [
        {
            "role": "tool",
            "name": "noop",
            "tool_call_id": f"call-recent-{i}",
            "turn_id": f"turn-recent-{i}",
            "status": "complete",
            "content": "{}",
        }
        for i in range(3)
    ]
    session = SessionModel(
        id="sess-multi",
        project_id="proj-multi",
        context_level="project",
        messages=old_turn + recent_turns,
    )
    db_session.add_all([project, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    hierarchy.tool_result_replay_turns = 3  # pushes turn-old out of the replay window

    pruned = [
        m for m in hierarchy.get_task_context(session)
        if m["role"] == "tool" and m["content"].startswith("[Pruned tool result]")
    ]
    assert len(pruned) == 2  # one summary per tool MESSAGE, not one per turn_id

    by_call_id = {m["tool_call_id"]: m for m in pruned}
    assert by_call_id.keys() == {"call-old-1", "call-old-2"}
    assert by_call_id["call-old-1"]["name"] == "update_task"
    assert by_call_id["call-old-2"]["name"] == "record_verdict"
    assert "CTV2-095" in by_call_id["call-old-1"]["content"]
    assert "CTV2-200" in by_call_id["call-old-2"]["content"]
    assert "changes_requested" in by_call_id["call-old-2"]["content"]

    # Regression guard: without tool_call_id, OpenAIAdapter demotes a "tool"
    # message to role="user", which then leaves the preceding assistant
    # tool_calls without a matching response and the provider replies 400.
    rendered = OpenAIAdapter.render_messages(pruned)
    assert all(item["role"] == "tool" for item in rendered)
    assert {item["tool_call_id"] for item in rendered} == {"call-old-1", "call-old-2"}


def test_build_messages_prefix_stable_across_task_mutation(db_session):
    """The entire pre-snapshot slice (Global + Project + append-only history,
    including a real assistant tool_calls <-> tool result pair) stays byte-
    identical across a task mutation; only the snapshot message changes.

    A ``messages=[]`` session does not exercise this: with no history the
    pre-snapshot slice is just Global+Project, which was already covered
    before CTV2-078 and does not prove history survives a mutation."""
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
    messages: list[dict] = []
    for i in range(4):
        messages.extend(
            _tool_turn(
                f"turn-{i}",
                call_id=f"call-{i}",
                name="update_task",
                content=json.dumps({"task_id": "CTV2-095", "verdict": "pass"}),
                final_text=f"Done with step {i}",
            )
        )
    session = SessionModel(
        id="sess-stable",
        task_id="TASK-STABLE-1",
        project_id="proj-stable",
        context_level="task",
        messages=messages,
    )
    db_session.add_all([project, task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    hierarchy.tool_result_replay_turns = 3  # fewer than the 4 turns above: turn-0 gets pruned
    before = hierarchy.build_messages(session)

    snapshot_index = next(i for i, m in enumerate(before) if "## System State" in m["content"])
    before_prefix = before[:snapshot_index]

    # Sanity: the slice under test actually contains real history, including
    # the assistant tool_calls <-> tool result pairing this AC cares about.
    assert len(before_prefix) > 4
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in before_prefix)
    assert any(m.get("role") == "tool" and m.get("tool_call_id") for m in before_prefix)

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
    after_snapshot_index = next(i for i, m in enumerate(after) if "## System State" in m["content"])
    after_prefix = after[:after_snapshot_index]

    assert after_snapshot_index == snapshot_index
    assert before_prefix == after_prefix  # whole pre-snapshot slice, not just a few elements

    before_hash = hashlib.sha256(json.dumps(before_prefix, sort_keys=True).encode()).hexdigest()
    after_hash = hashlib.sha256(json.dumps(after_prefix, sort_keys=True).encode()).hexdigest()
    assert before_hash == after_hash

    assert before[snapshot_index] != after[snapshot_index]  # Snapshot message changed
    assert "TASK-STABLE-2" in after[snapshot_index]["content"]
    assert "TASK-STABLE-2" not in before[snapshot_index]["content"]


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


def test_get_tool_definitions_returns_only_baseline_eager_tools(db_session):
    hierarchy = ContextHierarchy(db_session)
    tools = hierarchy.get_tool_definitions()
    names = {t["name"] for t in tools}

    assert names == {"create_task", "get_status", "query_db", "load_tools"}
    assert "dispatch_task" not in names
    assert "compact_context" not in names


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
