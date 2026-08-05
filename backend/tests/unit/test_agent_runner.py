import json
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.workers.agent_runner as runner
from app.db.base import Base
from app.db.models import (
    Agent,
    AgentOutputChunk,
    AgentRun,
    AuditLog,
    GateRecord,
    LLMUsage,
    Project,
    Session,
    Setting,
    Task,
    TaskDependency,
    TaskEvent,
    TaskRound,
)
from app.services.process_manager import (
    ProcessResult,
    ProcessStatus,
    WorktreeUnsupportedError,
)
from app.services.task_orchestration import BrakeDecision

FIXTURES = Path(__file__).parents[1] / "fixtures" / "review_results"


@pytest.fixture
def worker_db(monkeypatch, git_repo_root):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="project", name="Project", repo_root=git_repo_root))
    db.add(
        Task(
            id="RUN-001",
            project="project",
            title="Worker task",
            status="dispatched",
            executor="@test",
        )
    )
    db.add(
        AgentRun(
            id="run-001",
            task_id="RUN-001",
            agent_id="@test",
            cli="agy",
            command="echo output",
        )
    )
    db.commit()
    db.close()
    monkeypatch.setattr(runner, "SessionLocal", factory)
    monkeypatch.setattr(runner, "redis_client", MagicMock())
    monkeypatch.setattr(runner, "is_cancel_requested", MagicMock(return_value=False))
    monkeypatch.setattr(runner, "clear_cancel_request", MagicMock())
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_publish_line_format(monkeypatch):
    redis = MagicMock()
    monkeypatch.setattr(runner, "redis_client", redis)

    runner.publish_line("run-123", "test output", line_index=7)

    channel, payload = redis.publish.call_args.args
    data = json.loads(payload)
    assert channel == "agent_run:run-123:output"
    assert data["type"] == "stdout"
    assert data["content"] == "test output"
    assert data["index"] == 7
    assert "timestamp" in data


def test_publish_status_includes_extras(monkeypatch):
    redis = MagicMock()
    monkeypatch.setattr(runner, "redis_client", redis)

    runner.publish_status("run-123", "success", exit_code=0, result_ref="abc123")

    data = json.loads(redis.publish.call_args.args[1])
    assert data["status"] == "success"
    assert data["exit_code"] == 0
    assert data["result_ref"] == "abc123"


def test_publish_failure_is_retried_and_swallowed(monkeypatch):
    redis = MagicMock()
    redis.publish.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(runner, "redis_client", redis)
    monkeypatch.setattr(runner.time, "sleep", MagicMock())

    runner.publish_line("run-123", "output")

    assert redis.publish.call_count == 3


def test_missing_run_is_discarded(worker_db):
    assert runner.run_agent.fn("missing", "RUN-001", "echo test", "/tmp", 5) is None


def test_run_agent_persists_output_and_success(worker_db, git_repo_root):
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "printf 'first\\nsecond\\n' && echo change > change.txt "
        "&& git add change.txt && git commit -q -m change",
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == 0
    assert run.status == "success"
    assert run.failure_category == "unknown"
    assert run.failure_data_quality == "current"
    assert run.pid is None
    assert run.output_lines == 2
    assert run.output_bytes == len("firstsecond")
    assert [chunk.content for chunk in run.output_chunks] == ["first\nsecond"]
    task = db.get(Task, "RUN-001")
    assert task.status == "awaiting-review"
    base, sep, head = run.result_ref.partition("..")
    assert sep == ".."
    assert base and head and base != head
    assert task.result_ref == run.result_ref
    events = db.query(TaskEvent).filter_by(task_id=task.id).order_by(TaskEvent.id).all()
    assert [event.event_type for event in events] == ["running", "done"]
    assert events[0].payload["run_id"] == run.id
    assert isinstance(events[0].payload["pid"], int)
    assert events[1].payload == {
        "run_id": run.id,
        "result_ref": run.result_ref,
        "exit_code": 0,
    }
    db.close()


def test_json_cli_output_records_usage_and_keeps_result_ref_flow(
    worker_db, git_repo_root
):
    command = (
        "echo change > json-output.txt && git add json-output.txt "
        "&& git commit -q -m json-output "
        "&& ref=$(git rev-parse HEAD) "
        "&& printf '%s\\n' "
        "'{\"usage\":{\"input_tokens\":10,\"output_tokens\":4,"
        "\"cache_read_tokens\":2},\"result\":\"RESULT_REF: '"
        '"$ref"'
        "'\"}'"
    )
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        command,
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    usage = db.query(LLMUsage).one()
    assert result == 0
    assert run.status == "success"
    assert usage.agent_run_id == run.id
    assert usage.task_id == "RUN-001"
    assert usage.input_tokens == 12
    assert usage.output_tokens == 4
    assert usage.cached_tokens == 2
    assert run.result_ref and ".." in run.result_ref
    assert run.output_lines == 1
    assert run.agent_events[-1].event_type == "run.completed"
    db.close()


def test_failed_attempt_records_cli_usage_before_retry(worker_db, git_repo_root):
    output = json.dumps(
        {
            "usage": {
                "input_tokens": 398536,
                "output_tokens": 0,
                "cache_read_tokens": 0,
            },
            "result": "Error: timeout waiting for response",
        }
    )
    command = f"printf '%s\\n' '{output}' && exit 1"

    with pytest.raises(runner.AgentExecutionError, match="Exit code: 1"):
        runner.run_agent.fn("run-001", "RUN-001", command, git_repo_root, 5)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    usage = db.query(LLMUsage).one()
    assert run.status == "queued"
    assert usage.agent_run_id == run.id
    assert usage.input_tokens == 398536
    assert usage.output_tokens == 0
    db.close()


def _worktree_entries(repo_root: str) -> str:
    return subprocess.run(
        ["git", "worktree", "list"], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout


# ---------------------------------------------------------------------------
# run_agent: per-run git worktree isolation (CTV2-105)
# ---------------------------------------------------------------------------


def test_run_agent_executes_in_an_isolated_worktree_and_cleans_up(worker_db, git_repo_root):
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "echo change > change.txt && git add change.txt && git commit -q -m change",
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == 0
    assert run.status == "success"
    db.close()

    # No worktree left registered against the shared repo, and the commit
    # landed on the run's own branch -- never on the primary checkout.
    entries = _worktree_entries(git_repo_root)
    assert entries.count("[") == 1
    branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=git_repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch != "ct-run/run-001"
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=git_repo_root, check=True,
        capture_output=True, text=True,
    ).stdout
    base, _, head = run.result_ref.partition("..")
    assert head in log


def test_worktree_base_ref_uses_prior_head_for_re_dispatch(
    worker_db, monkeypatch, git_repo_root
):
    prior_head = runner._parse_result_ref(git_repo_root)
    assert prior_head
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.result_ref = f"{prior_head}..{prior_head}"
    db.commit()
    db.close()

    create_calls = []
    real_create = runner.WorktreeManager.create

    def capture_create(manager, run_id, base_ref):
        create_calls.append((run_id, base_ref))
        return real_create(manager, run_id, base_ref)

    monkeypatch.setattr(runner.WorktreeManager, "create", capture_create)

    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "echo change > change.txt && git add change.txt && git commit -q -m change",
        git_repo_root,
        5,
    )

    assert result == 0
    assert create_calls == [("run-001", prior_head)]


def test_run_agent_fails_closed_when_worktree_unsupported(
    worker_db, monkeypatch, git_repo_root
):
    monkeypatch.setattr(
        runner.WorktreeManager,
        "create",
        MagicMock(side_effect=WorktreeUnsupportedError("no git worktree support")),
    )
    initial_head = runner._parse_result_ref(git_repo_root)

    with pytest.raises(
        runner.AgentExecutionError,
        match="refusing to use the integration checkout",
    ):
        runner.run_agent.fn(
            "run-001",
            "RUN-001",
            "echo change > change.txt && git add change.txt && git commit -q -m change",
            git_repo_root,
            5,
        )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "queued"
    db.close()
    assert runner._parse_result_ref(git_repo_root) == initial_head
    assert not (Path(git_repo_root) / "change.txt").exists()


def test_run_agent_fails_closed_when_worktree_disabled_via_env(
    worker_db, monkeypatch, git_repo_root
):
    monkeypatch.setattr(runner, "WORKTREE_ENABLED", False)
    create = MagicMock()
    monkeypatch.setattr(runner.WorktreeManager, "create", create)
    initial_head = runner._parse_result_ref(git_repo_root)

    with pytest.raises(
        runner.AgentExecutionError,
        match="worktree isolation is disabled",
    ):
        runner.run_agent.fn(
            "run-001",
            "RUN-001",
            "echo change > change.txt && git add change.txt && git commit -q -m change",
            git_repo_root,
            5,
        )

    create.assert_not_called()
    assert runner._parse_result_ref(git_repo_root) == initial_head
    assert not (Path(git_repo_root) / "change.txt").exists()


def test_run_agent_cleans_up_worktree_after_a_failed_run(worker_db, git_repo_root):
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.max_attempts = 1
    db.commit()
    db.close()

    result = runner.run_agent.fn("run-001", "RUN-001", "exit 1", git_repo_root, 5)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == 1
    assert run.status == "failed"
    events = db.query(TaskEvent).filter_by(task_id=run.task_id).order_by(TaskEvent.id).all()
    assert [event.event_type for event in events] == ["running", "run_failed"]
    assert events[1].payload == {
        "run_id": run.id,
        "error": "Exit code: 1",
        "exit_code": 1,
    }
    db.close()
    assert _worktree_entries(git_repo_root).count("[") == 1


def test_run_agent_cancellation_leaves_no_worktree_or_lock(worker_db, monkeypatch, git_repo_root):
    monkeypatch.setattr(runner, "is_cancel_requested", MagicMock(return_value=True))

    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "sleep 30",
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "cancelled"
    db.close()
    assert _worktree_entries(git_repo_root).count("[") == 1
    assert not (Path(git_repo_root) / ".git" / "index.lock").exists()


def _force_no_progress_brake(monkeypatch):
    real_service = runner.TaskOrchestrationService

    class NoProgressService(real_service):
        def check_brakes(self, *_args, **_kwargs):
            return BrakeDecision(
                False,
                "Run made no progress within the allowed interval",
                "no_progress_limit",
            )

    monkeypatch.setattr(runner, "TaskOrchestrationService", NoProgressService)


def test_watchdog_cancelled_execute_run_records_failure_and_unsticks_task(
    worker_db, monkeypatch, git_repo_root
):
    _force_no_progress_brake(monkeypatch)

    assert runner.run_agent.fn(
        "run-001", "RUN-001", "echo should-not-run", git_repo_root, 5
    ) is None

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    task = db.get(Task, "RUN-001")
    failure = db.query(GateRecord).filter_by(
        task_id=task.id, gate_type="execution", status="rejected"
    ).one()
    assert run.status == "cancelled"
    assert task.status == "failed"
    assert "no_progress_limit" in task.error
    assert failure.output_ref == run.id
    assert failure.error_message == task.error
    db.close()


def test_watchdog_cancelled_review_run_returns_task_to_review_boundary(
    worker_db, monkeypatch, git_repo_root
):
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.kind = "review"
    run.agent_role = "reviewer"
    task = db.get(Task, "RUN-001")
    task.status = "in-review"
    task.result_ref = "base..head"
    task.reviewer = "@reviewer"
    db.commit()
    db.close()
    _force_no_progress_brake(monkeypatch)

    assert runner.run_agent.fn(
        "run-001", "RUN-001", "echo should-not-run", git_repo_root, 5
    ) is None

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    task = db.get(Task, "RUN-001")
    failure = db.query(GateRecord).filter_by(
        task_id=task.id, gate_type="review_result", status="rejected"
    ).one()
    assert run.status == "cancelled"
    assert task.status == "awaiting-review"
    assert task.current_gate == "review_order"
    assert task.result_ref == "base..head"
    assert "no_progress_limit" in task.error
    assert failure.output_ref == run.id
    assert failure.error_message == task.error
    db.close()


def test_two_concurrent_agent_runs_commit_independently(monkeypatch, git_repo_root):
    # Two fully independent DB engines, one per simulated worker thread: a
    # single sqlite connection isn't safe to hammer from two threads at
    # once, and that DB-layer detail is orthogonal to what this test is
    # proving -- that two run_agent invocations can commit to the *same git
    # repo* at the same time without contending on `.git/index.lock`.
    factories = {}

    def make_factory(task_id: str, run_id: str):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        db.add(Project(id="project", name="Project", repo_root=git_repo_root))
        db.add(Task(id=task_id, project="project", title="t", status="dispatched", executor="@test"))
        db.add(AgentRun(id=run_id, task_id=task_id, agent_id="@test", cli="agy", command="echo"))
        db.commit()
        db.close()
        return factory

    factories["run-a"] = make_factory("TASK-A", "run-a")
    factories["run-b"] = make_factory("TASK-B", "run-b")

    def dynamic_session_local():
        return factories[threading.current_thread().name]()

    monkeypatch.setattr(runner, "SessionLocal", dynamic_session_local)
    monkeypatch.setattr(runner, "redis_client", MagicMock())
    monkeypatch.setattr(runner, "is_cancel_requested", MagicMock(return_value=False))
    monkeypatch.setattr(runner, "clear_cancel_request", MagicMock())

    results = {}
    errors = []

    def call(run_id, task_id, label):
        try:
            results[run_id] = runner.run_agent.fn(
                run_id,
                task_id,
                f"echo {label} > {label}.txt && git add {label}.txt && git commit -q -m {label}",
                git_repo_root,
                10,
            )
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors.append(exc)

    t1 = threading.Thread(target=call, args=("run-a", "TASK-A", "from-a"), name="run-a")
    t2 = threading.Thread(target=call, args=("run-b", "TASK-B", "from-b"), name="run-b")
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors
    assert results == {"run-a": 0, "run-b": 0}

    db_a = factories["run-a"]()
    db_b = factories["run-b"]()
    run_a = db_a.get(AgentRun, "run-a")
    run_b = db_b.get(AgentRun, "run-b")
    assert run_a.status == "success"
    assert run_b.status == "success"
    head_a = run_a.result_ref.partition("..")[2]
    head_b = run_b.result_ref.partition("..")[2]
    assert head_a != head_b
    db_a.close()
    db_b.close()

    log = subprocess.run(
        ["git", "log", "--all", "--format=%H"], cwd=git_repo_root, check=True,
        capture_output=True, text=True,
    ).stdout
    assert head_a in log
    assert head_b in log
    assert not (Path(git_repo_root) / ".git" / "index.lock").exists()
    assert _worktree_entries(git_repo_root).count("[") == 1


def test_duplicate_delivery_same_run_only_one_attempt_runs(
    monkeypatch, git_repo_root, tmp_path
):
    """A redelivered Dramatiq message cannot start a second attempt/seq stream."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'worker.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="project", name="Project", repo_root=git_repo_root))
    db.add(Task(id="TASK-LOCK", project="project", title="locked", status="dispatched"))
    db.add(
        AgentRun(
            id="run-lock",
            task_id="TASK-LOCK",
            agent_id="@test",
            cli="agy",
            command="echo",
        )
    )
    db.commit()
    db.close()

    entered = threading.Event()
    release = threading.Event()
    manager = MagicMock()

    def stream(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        yield ProcessResult(ProcessStatus.COMPLETED, 0, None)

    manager.run_with_streaming.side_effect = stream
    monkeypatch.setattr(runner, "SessionLocal", factory)
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))
    monkeypatch.setattr(runner, "redis_client", MagicMock())
    monkeypatch.setattr(runner, "is_cancel_requested", MagicMock(return_value=False))
    monkeypatch.setattr(runner, "clear_cancel_request", MagicMock())

    first_errors = []

    def first_attempt():
        try:
            runner.run_agent.fn("run-lock", "TASK-LOCK", "echo", git_repo_root, 5)
        except Exception as exc:  # pragma: no cover - assertion below surfaces it
            first_errors.append(exc)

    thread = threading.Thread(target=first_attempt)
    thread.start()
    assert entered.wait(timeout=5)

    # The first attempt has committed its running claim; the duplicate must
    # return without entering ProcessManager or allocating event sequences.
    assert runner.run_agent.fn("run-lock", "TASK-LOCK", "echo", git_repo_root, 5) is None
    assert manager.run_with_streaming.call_count == 1

    release.set()
    thread.join(timeout=10)
    assert not first_errors

    db = factory()
    run = db.get(AgentRun, "run-lock")
    seqs = [event.seq for event in run.agent_events]
    assert seqs == list(range(len(seqs)))
    assert len(seqs) == len(set(seqs))
    db.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_concurrency_brake_queues_run_without_spawning_process(
    worker_db, monkeypatch, git_repo_root
):
    manager = MagicMock()
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    db = worker_db()
    db.add(Setting(key="max_concurrent_runs", value=2))
    db.add_all(
        [
            Task(id="RUN-002", project="project", title="Other", status="dispatched"),
            Task(id="RUN-003", project="project", title="Other 2", status="dispatched"),
        ]
    )
    db.add_all(
        [
            AgentRun(
                id="run-002",
                task_id="RUN-002",
                agent_id="@test",
                cli="agy",
                command="echo",
                status="running",
            ),
            AgentRun(
                id="run-003",
                task_id="RUN-003",
                agent_id="@test",
                cli="agy",
                command="echo",
                status="running",
            ),
        ]
    )
    db.commit()
    db.close()

    with pytest.raises(runner.AgentExecutionError):
        runner.run_agent.fn("run-001", "RUN-001", "echo test", git_repo_root, 5)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "queued"
    assert run.error_message and "Concurrent run limit" in run.error_message
    manager.run_with_streaming.assert_not_called()
    db.close()


def test_terminal_run_is_not_reevaluated_by_brakes(worker_db, monkeypatch, git_repo_root):
    manager = MagicMock()
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.status = "success"
    run.exit_code = 0
    db.add(Setting(key="autonomy_enabled", value=False))
    db.commit()
    db.close()

    result = runner.run_agent.fn("run-001", "RUN-001", "echo test", git_repo_root, 5)

    assert result == 0
    manager.run_with_streaming.assert_not_called()
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "success"
    db.close()


def test_run_without_commit_marks_no_changes_and_does_not_advance(worker_db, git_repo_root):
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "printf 'first\\nsecond\\n'",
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    task = db.get(Task, "RUN-001")
    assert result == 0
    assert run.status == "failed"
    assert "without committed changes" in run.error_message
    assert task.status == "failed"
    assert task.result_ref is None
    db.close()


def test_explicit_result_ref_outside_range_is_rejected(worker_db, git_repo_root):
    # The executor claims a ref that isn't reachable from base..head — must
    # not be trusted even though a real commit did land on HEAD.
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "echo change > change.txt && git add change.txt && git commit -q -m change "
        "&& echo 'result_ref: deadbeef'",
        git_repo_root,
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    task = db.get(Task, "RUN-001")
    assert result == 0
    assert run.status == "failed"
    assert "outside the actual base..head range" in run.error_message
    assert task.status == "failed"
    db.close()


def test_output_rolls_over_to_multiple_chunks(worker_db, monkeypatch, git_repo_root):
    monkeypatch.setattr(runner, "OUTPUT_CHUNK_LINES", 1)

    runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "printf 'first\\nsecond\\n'",
        git_repo_root,
        5,
    )

    db = worker_db()
    assert [chunk.content for chunk in db.get(AgentRun, "run-001").output_chunks] == [
        "first",
        "second",
    ]
    db.close()


def test_failed_agent_is_queued_for_retry(worker_db, monkeypatch, git_repo_root):
    manager = MagicMock()
    manager.pid = 123
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.FAILED, 2, "Exit code: 2")]
    )
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(runner.AgentExecutionError):
        runner.run_agent.fn("run-001", "RUN-001", "exit 2", git_repo_root, 5)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "queued"
    assert run.attempt == 1
    db.close()


def test_timeout_is_terminal_and_not_retried(worker_db, monkeypatch, git_repo_root):
    manager = MagicMock()
    manager.pid = 123
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.TIMEOUT, -1, "Timeout")]
    )
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    result = runner.run_agent.fn("run-001", "RUN-001", "sleep 60", git_repo_root, 1)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == -1
    assert run.status == "timeout"
    assert run.failure_category == "infra_timeout"
    assert run.attempt == 1
    db.close()


def test_empty_process_stream_becomes_retryable_failure(worker_db, monkeypatch, git_repo_root):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter([])
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(runner.AgentExecutionError, match="without a result"):
        runner.run_agent.fn("run-001", "RUN-001", "test", git_repo_root, 5)


def test_unexpected_error_is_retried(worker_db, monkeypatch, git_repo_root):
    manager = MagicMock()
    manager.run_with_streaming.side_effect = RuntimeError("unexpected")
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(RuntimeError, match="unexpected"):
        runner.run_agent.fn("run-001", "RUN-001", "test", git_repo_root, 5)

    db = worker_db()
    assert db.get(AgentRun, "run-001").status == "queued"
    db.close()


def test_unexpected_error_on_last_attempt_is_terminal(worker_db, monkeypatch, git_repo_root):
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.max_attempts = 1
    db.commit()
    db.close()
    manager = MagicMock()
    manager.run_with_streaming.side_effect = RuntimeError("terminal")
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    assert runner.run_agent.fn("run-001", "RUN-001", "test", git_repo_root, 5) is None

    db = worker_db()
    assert db.get(AgentRun, "run-001").status == "failed"
    assert db.get(Task, "RUN-001").status == "failed"
    db.close()


def test_current_attempt_uses_broker_retry_count(monkeypatch):
    run = MagicMock(attempt=1, started_at=None, status="queued")
    message = MagicMock(options={"retries": 2})
    monkeypatch.setattr(
        runner.CurrentMessage,
        "get_current_message",
        MagicMock(return_value=message),
    )

    assert runner._current_attempt(run) == 3


def test_cleanup_stale_process_group():
    process = subprocess.Popen(
        "sleep 60",
        shell=True,
        start_new_session=True,
    )
    run = MagicMock(
        id="stale-run",
        status="running",
        pid=process.pid,
        command="sleep 60",
    )

    runner._cleanup_stale_process(run, grace_seconds=0.2)
    process.wait(timeout=2)

    assert process.poll() is not None


def test_parse_result_ref_handles_spawn_error(monkeypatch):
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        MagicMock(side_effect=OSError("git missing")),
    )

    assert runner._parse_result_ref("/tmp") is None


def _commit_change(repo_root: str, message: str = "change") -> None:
    path = Path(repo_root) / f"{message}.txt"
    path.write_text(message)
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_root, check=True)


def test_build_execution_result_ref_returns_base_head_range(git_repo_root):
    base = runner._parse_result_ref(git_repo_root)
    _commit_change(git_repo_root)

    result_ref, error = runner._build_execution_result_ref(git_repo_root, base)

    assert error is None
    assert result_ref == f"{base}..{runner._parse_result_ref(git_repo_root)}"


def test_build_execution_result_ref_flags_no_committed_changes(git_repo_root):
    base = runner._parse_result_ref(git_repo_root)

    result_ref, error = runner._build_execution_result_ref(git_repo_root, base)

    assert result_ref is None
    assert "without committed changes" in error


def test_build_execution_result_ref_warns_on_dirty_repo(git_repo_root, monkeypatch):
    # Assert on the logger call directly rather than via caplog: some tests in
    # the wider suite invoke Alembic's fileConfig, which disables loggers not
    # named in alembic.ini and would otherwise silently swallow this warning.
    warning = MagicMock()
    monkeypatch.setattr(runner.logger, "warning", warning)
    base = runner._parse_result_ref(git_repo_root)
    _commit_change(git_repo_root)
    (Path(git_repo_root) / "untracked.txt").write_text("dirty")

    result_ref, error = runner._build_execution_result_ref(git_repo_root, base)

    assert error is None
    assert result_ref is not None
    assert warning.call_count == 1
    assert "uncommitted changes" in warning.call_args.args[0]


def test_build_execution_result_ref_uses_explicit_ref_when_head_unchanged(git_repo_root):
    """Explicit RESULT_REF from agent output should be used even when worktree HEAD hasn't moved."""
    base = runner._parse_result_ref(git_repo_root)
    _commit_change(git_repo_root, "explicit commit")
    explicit = runner._parse_result_ref(git_repo_root)
    # Simulate worktree scenario: reset HEAD to base but keep explicit commit reachable
    subprocess.run(["git", "checkout", "-q", base], cwd=git_repo_root, check=True)
    # Now HEAD == base, but explicit commit exists

    result_ref, error = runner._build_execution_result_ref(git_repo_root, base, explicit)

    assert error is None
    assert result_ref == f"{base}..{explicit}"


def test_run_base_ref_recovers_baseline_from_pending_range():
    assert runner._run_base_ref("abc123..") == "abc123"
    assert runner._run_base_ref("abc123..def456") == "abc123"
    assert runner._run_base_ref(None) is None
    assert runner._run_base_ref("no-range-here") is None


def test_extract_explicit_result_ref_matches_convention():
    assert runner._extract_explicit_result_ref("result_ref: abc123") == "abc123"
    assert runner._extract_explicit_result_ref("Result Reference: def456") == "def456"
    assert runner._extract_explicit_result_ref("just some output") is None


@pytest.mark.parametrize(
    ("fixture", "code"),
    [("missing_field.json", "missing_required_field"),
     ("wrong_type.json", "invalid_type"),
     ("empty.json", "empty_file")],
)
def test_load_review_result_rejects_invalid_fixtures(tmp_path, fixture, code):
    result_path = runner.review_result_path(str(tmp_path), "CTV2-102")
    Path(result_path).parent.mkdir()
    Path(result_path).write_bytes((FIXTURES / fixture).read_bytes())

    with pytest.raises(runner.ReviewResultLoadError) as error:
        runner.load_review_result(str(tmp_path), "CTV2-102", [])

    assert error.value.code == code
    assert error.value.as_dict()["code"] == code
    assert error.value.code != "pass"


def test_load_review_result_accepts_valid_fixture(tmp_path):
    result_path = runner.review_result_path(str(tmp_path), "CTV2-102")
    Path(result_path).parent.mkdir()
    Path(result_path).write_bytes((FIXTURES / "valid.json").read_bytes())

    result = runner.load_review_result(str(tmp_path), "CTV2-102", ["one", "two"])

    assert result.schema_version == "1.0"
    assert result.task_id == "CTV2-102"
    assert [item.verdict for item in result.ac_results] == ["pass", "pass"]


def test_load_review_result_counts_acceptance_plus_constraints(tmp_path):
    result_path = runner.review_result_path(str(tmp_path), "CTV2-102")
    Path(result_path).parent.mkdir()
    Path(result_path).write_bytes((FIXTURES / "valid.json").read_bytes())

    result = runner.load_review_result(
        str(tmp_path), "CTV2-102", ["positive outcome"], ["negative boundary"]
    )
    assert len(result.ac_results) == 2


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"toolchain_notes": "OCR was rate limited"},
            {"notes": "OCR was rate limited"},
        ),
        (
            {
                "toolchain_output": {"ocr": {"status": "completed_with_errors"}},
                "notes": "Git fixture lacked identity",
            },
            {
                "ocr": {"status": "completed_with_errors"},
                "notes": "Git fixture lacked identity",
            },
        ),
    ],
)
def test_load_review_result_accepts_observed_claude_metadata_aliases(
    tmp_path, metadata, expected
):
    """CTV2-1342/1345: preserve only the aliases emitted by real Opus runs."""
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload.update(metadata)
    result_path = Path(runner.review_result_path(str(tmp_path), "CTV2-102"))
    result_path.parent.mkdir()
    result_path.write_text(json.dumps(payload))

    result = runner.load_review_result(str(tmp_path), "CTV2-102", ["one", "two"])

    assert result.toolchain_results == expected


def test_load_review_result_schema_validation_keeps_queryable_pydantic_errors(
    tmp_path,
):
    payload = json.loads((FIXTURES / "valid.json").read_text())
    payload["invented_metadata"] = "must remain forbidden"
    result_path = Path(runner.review_result_path(str(tmp_path), "CTV2-102"))
    result_path.parent.mkdir()
    result_path.write_text(json.dumps(payload))

    with pytest.raises(runner.ReviewResultLoadError) as error:
        runner.load_review_result(str(tmp_path), "CTV2-102", ["one", "two"])

    assert error.value.code == "schema_validation"
    [detail] = error.value.details["errors"]
    assert detail["type"] == "extra_forbidden"
    assert detail["loc"] == ["invented_metadata"]
    assert detail["input"] == "must remain forbidden"
    json.dumps(error.value.as_dict())  # safe for JSON ledger/telemetry columns


def test_load_review_result_reports_acceptance_criteria_count_mismatch(tmp_path):
    payload = json.loads((FIXTURES / "valid.json").read_text())
    result_path = Path(runner.review_result_path(str(tmp_path), "CTV2-102"))
    result_path.parent.mkdir()
    result_path.write_text(json.dumps(payload))

    with pytest.raises(runner.ReviewResultLoadError) as error:
        runner.load_review_result(str(tmp_path), "CTV2-102", ["only one"])

    assert error.value.code == "acceptance_criteria_count_mismatch"
    assert error.value.details == {"expected": 1, "actual": 2}


def test_update_missing_task_is_not_silent(worker_db):
    db = worker_db()

    with pytest.raises(Exception, match="Task missing not found"):
        runner._update_task_status(db, "missing", "failed", error="error")

    db.close()


# ---------------------------------------------------------------------------
# advance_task: orchestration driver (CTV2-089)
# ---------------------------------------------------------------------------


@pytest.fixture
def driver_db(monkeypatch, tmp_path):
    import app.services.task_orchestration as orchestration

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="project", name="Project", repo_root=str(tmp_path)))
    db.add(Agent(id="@executor", name="Executor", role="executor", cli="codex", capabilities=["general"]))
    db.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex", capabilities=["general"]))
    db.commit()
    db.close()
    monkeypatch.setattr(runner, "SessionLocal", factory)
    context_checker = getattr(orchestration, "ContextChecker", None)
    if context_checker is not None:
        monkeypatch.setattr(
            context_checker,
            "check_project_ready",
            lambda self, project_id: {"ready": True},
        )
    run_agent_mock = MagicMock()
    run_agent_mock.send.return_value = MagicMock(message_id="msg-driver")
    monkeypatch.setattr(runner, "run_agent", run_agent_mock)
    yield factory, run_agent_mock
    Base.metadata.drop_all(engine)
    engine.dispose()


def _driver_task(factory, task_id, **overrides):
    db = factory()
    fields = {
        "id": task_id,
        "project": "project",
        "title": "Driver task",
        "status": "todo",
        "mode": "bypass",
    }
    fields.update(overrides)
    db.add(Task(**fields))
    db.commit()
    db.close()


def test_advance_task_missing_task_is_a_noop(driver_db):
    factory, run_agent_mock = driver_db
    assert runner.advance_task.fn("MISSING", "manual") == "not_found"
    run_agent_mock.send.assert_not_called()


def test_advance_task_todo_missing_ac_escalates_fail_closed(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-001", acceptance_criteria=[])

    outcome = runner.advance_task.fn("ADV-001", "manual")

    assert outcome == "escalated_missing_ac"
    db = factory()
    task = db.get(Task, "ADV-001")
    # Escalation asks a human to act; it must not mark the task terminal.
    # `failed` cancelled the active runs and rejected the pending gates,
    # so the prompt the escalation had just written could never be
    # answered (CTV2-1382 / CTV2-1388, 2026-08-05).  `awaiting_approval`
    # is now the only thing stopping the loop, so it has to survive.
    assert task.status == "todo"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_todo_with_ac_dispatches_the_best_matched_executor(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-002", acceptance_criteria=["Tests pass"])

    outcome = runner.advance_task.fn("ADV-002", "manual")

    assert outcome == "dispatched"
    db = factory()
    task = db.get(Task, "ADV-002")
    assert task.status == "dispatched"
    assert task.executor == "@executor"
    assert db.query(AgentRun).filter(AgentRun.task_id == "ADV-002").count() == 1
    db.close()
    run_agent_mock.send.assert_called_once()


def test_advance_task_todo_with_constraints_only_passes_fail_closed_gate(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-CONSTRAINTS",
        acceptance_criteria=[],
        constraints=["Do not change the public API"],
    )

    outcome = runner.advance_task.fn("ADV-CONSTRAINTS", "manual")

    assert outcome == "dispatched"
    db = factory()
    assert db.get(Task, "ADV-CONSTRAINTS").status == "dispatched"
    db.close()
    run_agent_mock.send.assert_called_once()


def test_advance_task_supervised_todo_stops_at_gate_pending_and_never_loops(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-003", mode="supervised", acceptance_criteria=["Tests pass"])

    outcome = runner.advance_task.fn("ADV-003", "manual")
    assert outcome == "gate_pending"

    db = factory()
    task = db.get(Task, "ADV-003")
    assert task.status == "todo"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()

    for _ in range(runner.AUTO_MAX_ROUNDS + 2):
        assert runner.advance_task.fn("ADV-003", "manual") == "gate_pending"

    db = factory()
    task = db.get(Task, "ADV-003")
    assert task.status == "todo"  # never silently escalated while parked for approval
    db.close()


def test_advance_task_respects_autonomy_kill_switch(driver_db):
    factory, run_agent_mock = driver_db
    db = factory()
    db.add(Setting(key="autonomy_enabled", value=False))
    db.commit()
    db.close()
    _driver_task(factory, "ADV-004", acceptance_criteria=["Tests pass"])

    outcome = runner.advance_task.fn("ADV-004", "manual")

    assert outcome == "brake:autonomy_disabled"
    db = factory()
    task = db.get(Task, "ADV-004")
    assert task.status == "failed"
    assert task.awaiting_approval is False
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_awaiting_review_picks_independent_reviewer(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-005",
        status="awaiting-review",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
        result_ref="base123..head456",
    )

    outcome = runner.advance_task.fn("ADV-005", "manual")

    assert outcome == "review_requested"
    db = factory()
    task = db.get(Task, "ADV-005")
    assert task.status == "in-review"
    assert task.reviewer == "@reviewer"
    db.close()
    run_agent_mock.send.assert_called_once()


def test_advance_task_review_gate_explains_reviewer_selection(driver_db):
    factory, run_agent_mock = driver_db
    db = factory()
    db.add(
        Agent(
            id="@disabled-reviewer",
            name="Disabled Reviewer",
            role="reviewer",
            cli="codex",
            status="disabled",
            capabilities=["general"],
        )
    )
    db.commit()
    db.close()
    _driver_task(
        factory,
        "ADV-REVIEW-PROMPT",
        status="awaiting-review",
        mode="supervised",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
        result_ref="base123..head456",
    )

    outcome = runner.advance_task.fn("ADV-REVIEW-PROMPT", "manual")

    assert outcome == "gate_pending"
    db = factory()
    task = db.get(Task, "ADV-REVIEW-PROMPT")
    gate = db.query(GateRecord).filter_by(
        task_id=task.id, gate_type="review_order", status="pending"
    ).one()
    reason = gate.input_payload["selection_reason"]
    assert gate.input_payload["reviewer"] == "@reviewer"
    assert "capability match=" in reason
    assert "success_rate=" in reason
    assert "@executor (four-eyes)" in reason
    assert "@disabled-reviewer (disabled)" in reason
    assert task.approval_prompt == gate.input_payload["approval_prompt"]
    assert "Reviewer đề xuất: @reviewer" in task.approval_prompt
    assert reason in task.approval_prompt
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_awaiting_review_waits_for_result_ref(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-006",
        status="awaiting-review",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
        result_ref=None,
    )

    outcome = runner.advance_task.fn("ADV-006", "manual")

    assert outcome == "waiting_result_ref"
    run_agent_mock.send.assert_not_called()


def test_advance_task_changes_requested_redispatches_under_round_cap(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-007",
        status="changes-requested",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
    )

    outcome = runner.advance_task.fn("ADV-007", "manual")

    assert outcome == "dispatched"
    db = factory()
    task = db.get(Task, "ADV-007")
    assert task.status == "dispatched"
    db.close()
    run_agent_mock.send.assert_called_once()


def test_advance_task_changes_requested_escalates_at_round_cap(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-008",
        status="changes-requested",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
    )
    db = factory()
    for i in range(runner.AUTO_MAX_ROUNDS):
        db.add(
            TaskRound(
                task_id="ADV-008",
                round_no=i + 1,
            )
        )
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-008", "manual")

    assert outcome == "escalated_round_limit"
    db = factory()
    task = db.get(Task, "ADV-008")
    # Escalation asks a human to act; it must not mark the task terminal.
    # `failed` cancelled the active runs and rejected the pending gates,
    # so the prompt the escalation had just written could never be
    # answered (CTV2-1382 / CTV2-1388, 2026-08-05).  `awaiting_approval`
    # is now the only thing stopping the loop, so it has to survive.
    assert task.status == "changes-requested"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_changes_requested_escalates_at_custom_policy_round_cap(driver_db):
    factory, run_agent_mock = driver_db
    db = factory()
    proj = Project(id="POLICY-PROJ", name="Policy", autonomy_policy={"auto_max_rounds": 1})
    db.add(proj)
    db.commit()
    db.close()

    _driver_task(
        factory,
        "ADV-POLICY-ROUND",
        project="POLICY-PROJ",
        status="changes-requested",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
    )
    db = factory()
    db.add(
        TaskRound(
            task_id="ADV-POLICY-ROUND",
            round_no=1,
        )
    )
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-POLICY-ROUND", "manual")

    assert outcome == "escalated_round_limit"
    db = factory()
    task = db.get(Task, "ADV-POLICY-ROUND")
    # Escalation asks a human to act; it must not mark the task terminal.
    # `failed` cancelled the active runs and rejected the pending gates,
    # so the prompt the escalation had just written could never be
    # answered (CTV2-1382 / CTV2-1388, 2026-08-05).  `awaiting_approval`
    # is now the only thing stopping the loop, so it has to survive.
    assert task.status == "changes-requested"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_enforces_plan_local_round_limit(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-PLAN-ROUND",
        status="changes-requested",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
        limits={"max_execution_rounds": 1, "max_tokens": 100_000},
    )
    db = factory()
    db.add(TaskRound(task_id="ADV-PLAN-ROUND", round_no=1))
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-PLAN-ROUND", "manual")

    assert outcome == "escalated_round_limit"
    run_agent_mock.send.assert_not_called()



def test_advance_task_terminal_statuses_are_a_noop(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-009",
        status="done",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base123..head456",
        acceptance_criteria=["Tests pass"],
    )
    _driver_task(factory, "ADV-010", status="failed", acceptance_criteria=["Tests pass"])

    assert runner.advance_task.fn("ADV-009", "manual") == "terminal"
    assert runner.advance_task.fn("ADV-010", "manual") == "terminal"
    run_agent_mock.send.assert_not_called()


def test_advance_task_dispatched_and_in_review_wait_for_the_run(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-011", status="dispatched", executor="@executor")
    _driver_task(factory, "ADV-012", status="in-review", executor="@executor", reviewer="@reviewer")

    assert runner.advance_task.fn("ADV-011", "manual") == "waiting"
    assert runner.advance_task.fn("ADV-012", "manual") == "waiting"
    run_agent_mock.send.assert_not_called()


def test_advance_task_stalled_actionable_status_escalates_instead_of_looping(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-013",
        status="awaiting-review",
        executor="@executor",
        acceptance_criteria=["Tests pass"],
        result_ref=None,
    )

    outcomes = [runner.advance_task.fn("ADV-013", "manual") for _ in range(runner.AUTO_MAX_ROUNDS)]
    assert outcomes == ["waiting_result_ref"] * runner.AUTO_MAX_ROUNDS

    outcome = runner.advance_task.fn("ADV-013", "manual")

    assert outcome == "escalated_stall"
    db = factory()
    task = db.get(Task, "ADV-013")
    # Escalation asks a human to act; it must not mark the task terminal.
    # `failed` cancelled the active runs and rejected the pending gates,
    # so the prompt the escalation had just written could never be
    # answered (CTV2-1382 / CTV2-1388, 2026-08-05).  `awaiting_approval`
    # is now the only thing stopping the loop, so it has to survive.
    assert task.status == "awaiting-review"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_dispatch_idempotency_key_prevents_duplicate_run(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-014", acceptance_criteria=["Tests pass"])

    first = runner.advance_task.fn("ADV-014", "manual")
    assert first == "dispatched"

    # Simulate a redelivered/duplicate advance_task trigger racing back to
    # "todo" before the first dispatch's downstream effects are visible --
    # the driver must not mint a second AgentRun for the same round.
    db = factory()
    task = db.get(Task, "ADV-014")
    task.status = "todo"
    db.commit()
    db.close()

    runner.advance_task.fn("ADV-014", "manual")

    db = factory()
    assert db.query(AgentRun).filter(AgentRun.task_id == "ADV-014").count() == 1
    db.close()


def test_advance_task_audit_trail_records_every_call(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-015", acceptance_criteria=["Tests pass"])

    runner.advance_task.fn("ADV-015", "run_agent_completed")

    db = factory()
    entries = db.query(AuditLog).filter(AuditLog.action == "advance_task:run_agent_completed").all()
    assert len(entries) == 1
    assert entries[0].details["status_before"] == "todo"
    assert entries[0].details["status_after"] == "dispatched"
    assert entries[0].details["outcome"] == "dispatched"
    db.close()


# ---------------------------------------------------------------------------
# advance_task: task_dependencies gate + wake-up (CTV2-094)
# ---------------------------------------------------------------------------


def test_advance_task_todo_waits_for_unmet_dependency(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-UP", status="dispatched", acceptance_criteria=["Tests pass"])
    _driver_task(factory, "ADV-DOWN", acceptance_criteria=["Tests pass"])
    db = factory()
    db.add(TaskDependency(task_id="ADV-DOWN", depends_on_task_id="ADV-UP"))
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-DOWN", "manual")

    assert outcome == "waiting_dependency"
    db = factory()
    task = db.get(Task, "ADV-DOWN")
    assert task.status == "todo"
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_todo_dispatches_once_dependency_is_done(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(
        factory,
        "ADV-UP2",
        status="done",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base..head",
        acceptance_criteria=["Tests pass"],
    )
    _driver_task(factory, "ADV-DOWN2", acceptance_criteria=["Tests pass"])
    db = factory()
    db.add(TaskDependency(task_id="ADV-DOWN2", depends_on_task_id="ADV-UP2"))
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-DOWN2", "manual")

    assert outcome == "dispatched"
    db = factory()
    task = db.get(Task, "ADV-DOWN2")
    assert task.status == "dispatched"
    db.close()
    run_agent_mock.send.assert_called_once()


def test_advance_task_escalates_when_dependency_failed(driver_db):
    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-UP3", status="failed", acceptance_criteria=["Tests pass"])
    _driver_task(factory, "ADV-DOWN3", acceptance_criteria=["Tests pass"])
    db = factory()
    db.add(TaskDependency(task_id="ADV-DOWN3", depends_on_task_id="ADV-UP3"))
    db.commit()
    db.close()

    outcome = runner.advance_task.fn("ADV-DOWN3", "manual")

    assert outcome == "escalated_dependency_failed"
    db = factory()
    task = db.get(Task, "ADV-DOWN3")
    # Escalation asks a human to act; it must not mark the task terminal.
    # `failed` cancelled the active runs and rejected the pending gates,
    # so the prompt the escalation had just written could never be
    # answered (CTV2-1382 / CTV2-1388, 2026-08-05).  `awaiting_approval`
    # is now the only thing stopping the loop, so it has to survive.
    assert task.status == "todo"
    assert task.awaiting_approval is True
    db.close()
    run_agent_mock.send.assert_not_called()


def test_advance_task_does_not_wake_dependents_on_a_mere_escalation(driver_db):
    """An escalated task is blocked, not dead, so dependents keep waiting.

    `wake_dependents` exists to release tasks whose upstream can never
    complete.  While escalation implied `failed`, this test asserted the
    opposite -- an upstream that only needed a human to add acceptance
    criteria would tell its dependents to stop waiting for it.

    Escalation no longer marks the task terminal (CTV2-1382 / CTV2-1388), so
    the correct behaviour is the one asserted here: nothing is woken, because
    ADV-UP4 is still going to run once a human answers.
    """

    factory, run_agent_mock = driver_db
    _driver_task(factory, "ADV-UP4", acceptance_criteria=[])
    _driver_task(factory, "ADV-DOWN4", acceptance_criteria=["Tests pass"])
    db = factory()
    db.add(TaskDependency(task_id="ADV-DOWN4", depends_on_task_id="ADV-UP4"))
    db.commit()
    db.close()

    real_advance_fn = runner.advance_task.fn
    with patch.object(runner, "advance_task") as mocked_advance:
        outcome = real_advance_fn("ADV-UP4", "manual")

    assert outcome == "escalated_missing_ac"
    mocked_advance.send.assert_not_called()

    db = factory()
    upstream = db.get(Task, "ADV-UP4")
    assert upstream.status == "todo"
    assert upstream.awaiting_approval is True
    db.close()


def _dead_message(run_id="run-001", message_id="msg-dead-1", traceback="boom: RuntimeError"):
    return {
        "message_id": message_id,
        "args": (run_id, "RUN-001", "echo test", "/tmp", 900),
        "kwargs": {},
        "options": {"traceback": traceback},
    }


def test_dead_letter_fails_run_and_escalates_task(worker_db):
    outcome = runner.run_agent_dead_letter.fn(
        _dead_message(), {"retries": 3, "max_retries": 3}
    )

    assert outcome == "handled"
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    task = db.get(Task, "RUN-001")
    assert run.status == "failed"
    assert "dead-lettered after 3/3 retries" in run.error_message
    assert "boom: RuntimeError" in run.error_message
    assert task.status == "todo"
    db.close()


def test_dead_letter_is_idempotent_on_redelivery(worker_db):
    first = runner.run_agent_dead_letter.fn(
        _dead_message(), {"retries": 3, "max_retries": 3}
    )
    second = runner.run_agent_dead_letter.fn(
        _dead_message(), {"retries": 3, "max_retries": 3}
    )

    assert first == "handled"
    assert second == "discarded_resolved"
    db = worker_db()
    assert db.get(AgentRun, "run-001").status == "failed"
    db.close()


def test_dead_letter_ignores_message_for_unknown_run(worker_db):
    outcome = runner.run_agent_dead_letter.fn(
        _dead_message(run_id="missing-run"), {"retries": 1, "max_retries": 1}
    )

    assert outcome == "discarded_orphan"


def test_dead_letter_ignores_message_with_no_run_id(worker_db):
    dead_message = _dead_message()
    dead_message["args"] = ()

    outcome = runner.run_agent_dead_letter.fn(dead_message, {"retries": 1, "max_retries": 1})

    assert outcome == "discarded_no_run_id"
