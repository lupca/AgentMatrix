import hashlib
import json

import pytest
from app.db.models import (
    Agent,
    Project,
    SessionEventCursor,
    Task,
    TaskEvent,
)
from app.db.models import (
    Session as SessionModel,
)
from app.graph.context import build_context_snapshot, invalidate_context_snapshot
from app.services.command_router import CommandRouter
from app.services.context_hierarchy import PROJECT_CONTEXT_MAX_CHARS, ContextHierarchy
from app.services.coordinator import CoordinatorService
from app.services.llm_client import UsageCounts
from app.services.providers import ProviderResponse
from app.services.providers.openai_adapter import OpenAIAdapter


class _DigestProvider:
    name = "openai"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def complete(
        self,
        messages,
        model,
        stream=False,
        *,
        max_tokens=2048,
        temperature=0.7,
        tools=None,
    ):
        self.calls.append(list(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResponse(
            provider=self.name,
            model=model,
            text=str(outcome),
            usage=UsageCounts(input_tokens=10, output_tokens=5),
            request_id=f"digest-request-{len(self.calls)}",
            stop_reason="stop",
        )


def _digest_message(messages):
    return next(
        (
            message["content"]
            for message in messages
            if message.get("kind") == "task_event_digest"
        ),
        None,
    )


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

    compacted = hierarchy.compact_context(
        session, threshold=50, summary="Confirmed decision: retain CTV2-096 result_ref abc123."
    )
    assert compacted is True
    assert len(session.messages) == 11  # 1 summary msg + 10 kept
    assert session.messages[0]["role"] == "system"
    assert "CTV2-096" in session.messages[0]["content"]


def test_context_compaction_keeps_tool_call_pairs_through_adapter(db_session):
    """Compaction must not leave provider-invalid assistant/tool history."""
    first_call = "call-before-cutoff"
    second_call = "call-after-cutoff"
    messages = [
        {"role": "user", "content": "requests", "status": "complete"},
        {
            "role": "assistant", "content": "", "status": "complete",
            "tool_calls": [
                {"id": first_call, "name": "get_status", "input": {}},
                {"id": second_call, "name": "get_status", "input": {}},
            ],
        },
        {
            "role": "tool", "tool_call_id": first_call, "name": "get_status",
            "content": '{"task_id": "CTV2-095", "verdict": "pass"}',
            "status": "complete",
        },
        {
            "role": "tool", "tool_call_id": second_call, "name": "get_status",
            "content": '{"task_id": "CTV2-104", "verdict": "pass"}',
            "status": "complete",
        },
    ] + [
        {"role": "user", "content": f"filler-{i}", "status": "complete"}
        for i in range(8)
    ]
    session = SessionModel(id="sess-tool-compact", messages=messages)
    db_session.add(session)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    assert hierarchy.compact_context(
        session, threshold=0, summary="CTV2-095 verdict pass; CTV2-104 verdict pass."
    ) is True

    rendered = OpenAIAdapter.render_messages(session.messages)
    assistant_ids = {
        call["id"]
        for message in rendered
        if message["role"] == "assistant"
        for call in message.get("tool_calls", [])
    }
    tool_ids = {
        message["tool_call_id"]
        for message in rendered
        if message["role"] == "tool"
    }
    assert assistant_ids == tool_ids == {first_call, second_call}
    assert "CTV2-095" in next(m["content"] for m in rendered if m.get("tool_call_id") == first_call)

    # Pair-preserving expansion retained both tool results and the injected
    # summary preserves their decisions.
    assert "CTV2-104" in session.messages[0]["content"]


def test_context_compaction_uses_token_window_and_llm_summary(db_session):
    session = SessionModel(
        id="sess-token-compact",
        messages=[
            {"role": "user", "content": f"old-{i} task CTV2-096 " + ("detail " * 20), "status": "complete"}
            for i in range(20)
        ],
    )
    db_session.add(session)
    db_session.commit()
    original = list(session.messages)
    prompts = []

    def summarize(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return "Decision retained: task CTV2-096 verdict pass, result_ref ref-096."

    hierarchy = ContextHierarchy(db_session)
    assert hierarchy.compact_context(
        session, context_window=100, threshold_ratio=0.5, summarizer=summarize
    ) is True
    assert len(prompts) == 1
    assert "CTV2-096" in prompts[0]
    assert "result_ref" in session.messages[0]["content"]
    assert session.messages[0]["content"] != "[Context Compaction]"
    assert session.messages[1:] == original[-10:]


def test_context_compaction_failure_keeps_history_unchanged(db_session):
    session = SessionModel(
        id="sess-failed-compact",
        messages=[
            {"role": "user", "content": f"message-{i}", "status": "complete"}
            for i in range(20)
        ],
    )
    db_session.add(session)
    db_session.commit()
    original = list(session.messages)

    def fail(*args, **kwargs):
        raise RuntimeError("summarizer unavailable")

    hierarchy = ContextHierarchy(db_session)
    assert hierarchy.compact_context(
        session, context_window=100, threshold_ratio=0.5, summarizer=fail
    ) is False
    assert session.messages == original


def test_get_tool_definitions_returns_only_baseline_eager_tools(db_session):
    hierarchy = ContextHierarchy(db_session)
    tools = hierarchy.get_tool_definitions()
    names = {t["name"] for t in tools}

    assert names == {"create_task", "manage_inbox", "get_status", "get_run_output", "get_stats", "query_db", "load_tools"}
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


def test_task_context_enriches_with_persisted_gate_state(db_session):
    task = Task(
        id="GRAPH-1", project="proj-graph", title="Graph Task", status="done",
        current_gate="verdict", verdict="pass", findings=["all good"],
        executor="@executor", reviewer="@reviewer", result_ref="base..head",
    )
    session = SessionModel(
        id="sess-graph",
        task_id="GRAPH-1",
        project_id="proj-graph",
        context_level="task",
        messages=[],
    )
    db_session.add_all([task, session])
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    task_ctx = hierarchy.get_task_context(session)

    assert len(task_ctx) == 1
    assert "[Task Gate State]" in task_ctx[0]["content"]
    assert "verdict=pass" in task_ctx[0]["content"]
    assert "findings=all good" in task_ctx[0]["content"]


def test_task_context_without_gate_record_is_still_enriched(db_session):
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
    assert "[Task Gate State]" in task_ctx[0]["content"]


@pytest.mark.asyncio
async def test_compact_command_via_router(db_session, monkeypatch):
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
    assert result["compacted"] is False

    db_session.refresh(session)
    assert len(session.messages) == 15


@pytest.mark.asyncio
async def test_closed_session_replays_events_after_cursor_then_advances(db_session):
    """Cursor 2 replays exactly event IDs 3-6 once after a session reopens."""
    task = Task(
        id="TASK-REPLAY",
        project="proj-replay",
        title="Replay Task",
        status="todo",
    )
    session = SessionModel(
        id="sess-replay-events",
        task_id=task.id,
        project_id=task.project,
        context_level="task",
        status="closed",
        messages=[],
    )
    events = [
        TaskEvent(
            task_id=task.id,
            event_type="progress",
            kind="info",
            payload={"sequence": sequence},
        )
        for sequence in range(1, 7)
    ]
    db_session.add_all([task, session, *events])
    db_session.flush()
    assert [event.id for event in events] == [1, 2, 3, 4, 5, 6]
    cursor = SessionEventCursor(
        session_id=session.id,
        last_digest_event_id=2,
    )
    db_session.add(cursor)
    db_session.commit()

    provider = _DigestProvider(["first response", "second response"])
    coordinator = CoordinatorService(
        db_session,
        providers={"openai": provider},
        retry_base_seconds=0,
    )
    await coordinator.complete_turn(
        session,
        "What happened while this session was closed?",
        model="gpt-4o",
        idempotency_key="replay-turn-1",
    )

    first_digest = _digest_message(provider.calls[0])
    assert first_digest is not None
    assert "event #1 " not in first_digest
    assert "event #2 " not in first_digest
    positions = [first_digest.index(f"event #{event_id} ") for event_id in range(3, 7)]
    assert positions == sorted(positions)
    db_session.refresh(cursor)
    assert cursor.last_digest_event_id == 6

    await coordinator.complete_turn(
        session,
        "Anything else?",
        model="gpt-4o",
        idempotency_key="replay-turn-2",
    )
    assert _digest_message(provider.calls[1]) is None
    db_session.refresh(cursor)
    assert cursor.last_digest_event_id == 6


@pytest.mark.asyncio
async def test_failed_turn_keeps_cursor_and_replays_same_digest(db_session):
    task = Task(
        id="TASK-RETRY-DIGEST",
        project="proj-retry-digest",
        title="Retry Digest Task",
        status="todo",
    )
    session = SessionModel(
        id="sess-retry-digest",
        task_id=task.id,
        project_id=task.project,
        context_level="task",
        messages=[],
    )
    events = [
        TaskEvent(
            task_id=task.id,
            event_type="progress",
            kind="info",
            payload={"sequence": sequence},
        )
        for sequence in range(1, 4)
    ]
    cursor = SessionEventCursor(
        session_id=session.id,
        last_digest_event_id=0,
    )
    db_session.add_all([task, session, *events, cursor])
    db_session.commit()

    provider = _DigestProvider([RuntimeError("provider failed"), "recovered"])
    coordinator = CoordinatorService(
        db_session,
        providers={"openai": provider},
        max_retries=0,
        retry_base_seconds=0,
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        await coordinator.complete_turn(
            session,
            "Read these events",
            model="gpt-4o",
            idempotency_key="failed-digest-turn",
        )
    db_session.refresh(cursor)
    assert cursor.last_digest_event_id == 0
    failed_digest = _digest_message(provider.calls[0])
    assert failed_digest is not None

    await coordinator.complete_turn(
        session,
        "Retry reading these events",
        model="gpt-4o",
        idempotency_key="successful-digest-turn",
    )
    assert _digest_message(provider.calls[1]) == failed_digest
    db_session.refresh(cursor)
    assert cursor.last_digest_event_id == events[-1].id


def test_remove_rollup_logic_from_session_messages(db_session):
    """AC3: Legacy task_rollup messages in session.messages are excluded from replay."""
    session = SessionModel(
        id="sess-legacy-rollup",
        messages=[
            {"id": "msg-user", "role": "user", "content": "Start task", "status": "complete"},
            {
                "id": "task-rollup-1",
                "role": "system",
                "kind": "task_rollup",
                "content": '{"task_id": "TASK-OLD", "status": "done"}',
                "status": "complete",
            },
            {"id": "msg-assistant", "role": "assistant", "content": "Working on it", "status": "complete"},
        ],
    )
    db_session.add(session)
    db_session.commit()

    hierarchy = ContextHierarchy(db_session)
    replayed = hierarchy._replay_session_messages(session)

    assert len(replayed) == 2
    assert all(m.get("kind") != "task_rollup" for m in replayed)


def test_integration_llm_receives_task_events_in_context(db_session):
    """AC4: Integration test: LLM context receives task events emitted via TaskEventService."""
    from app.services.task_event_service import TaskEventService

    task = Task(id="TASK-INT-1", project="proj-int", title="Integration Task", status="todo")
    session = SessionModel(
        id="sess-int-events",
        task_id="TASK-INT-1",
        project_id="proj-int",
        context_level="task",
        messages=[],
    )
    db_session.add_all([task, session])
    db_session.commit()

    TaskEventService.emit("TASK-INT-1", "dispatched", {"executor": "@antigravity"}, db=db_session)
    TaskEventService.emit("TASK-INT-1", "done", {"result_ref": "abc123def"}, db=db_session)

    hierarchy = ContextHierarchy(db_session)
    messages = hierarchy.build_messages(session)

    digest = _digest_message(messages)
    assert digest is not None
    assert "dispatched" in digest
    assert "done" in digest
    assert "abc123def" in digest
