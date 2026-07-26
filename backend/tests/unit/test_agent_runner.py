import json
import subprocess
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.workers.agent_runner as runner
from app.db.base import Base
from app.db.models import AgentOutputChunk, AgentRun, Project, Task
from app.services.process_manager import ProcessResult, ProcessStatus


@pytest.fixture
def worker_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="project", name="Project", repo_root="/tmp"))
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


def test_run_agent_persists_output_and_success(worker_db):
    result = runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "printf 'first\\nsecond\\n'",
        "/tmp",
        5,
    )

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == 0
    assert run.status == "success"
    assert run.pid is None
    assert run.output_lines == 2
    assert run.output_bytes == len("firstsecond")
    assert [chunk.content for chunk in run.output_chunks] == ["first\nsecond"]
    assert db.get(Task, "RUN-001").status == "awaiting-review"
    db.close()


def test_output_rolls_over_to_multiple_chunks(worker_db, monkeypatch):
    monkeypatch.setattr(runner, "OUTPUT_CHUNK_LINES", 1)

    runner.run_agent.fn(
        "run-001",
        "RUN-001",
        "printf 'first\\nsecond\\n'",
        "/tmp",
        5,
    )

    db = worker_db()
    assert [chunk.content for chunk in db.get(AgentRun, "run-001").output_chunks] == [
        "first",
        "second",
    ]
    db.close()


def test_failed_agent_is_queued_for_retry(worker_db, monkeypatch):
    manager = MagicMock()
    manager.pid = 123
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.FAILED, 2, "Exit code: 2")]
    )
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(runner.AgentExecutionError):
        runner.run_agent.fn("run-001", "RUN-001", "exit 2", "/tmp", 5)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert run.status == "queued"
    assert run.attempt == 1
    db.close()


def test_timeout_is_terminal_and_not_retried(worker_db, monkeypatch):
    manager = MagicMock()
    manager.pid = 123
    manager.run_with_streaming.return_value = iter(
        [ProcessResult(ProcessStatus.TIMEOUT, -1, "Timeout")]
    )
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    result = runner.run_agent.fn("run-001", "RUN-001", "sleep 60", "/tmp", 1)

    db = worker_db()
    run = db.get(AgentRun, "run-001")
    assert result == -1
    assert run.status == "timeout"
    assert run.attempt == 1
    db.close()


def test_empty_process_stream_becomes_retryable_failure(worker_db, monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.return_value = iter([])
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(runner.AgentExecutionError, match="without a result"):
        runner.run_agent.fn("run-001", "RUN-001", "test", "/tmp", 5)


def test_unexpected_error_is_retried(worker_db, monkeypatch):
    manager = MagicMock()
    manager.run_with_streaming.side_effect = RuntimeError("unexpected")
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    with pytest.raises(RuntimeError, match="unexpected"):
        runner.run_agent.fn("run-001", "RUN-001", "test", "/tmp", 5)

    db = worker_db()
    assert db.get(AgentRun, "run-001").status == "queued"
    db.close()


def test_unexpected_error_on_last_attempt_is_terminal(worker_db, monkeypatch):
    db = worker_db()
    run = db.get(AgentRun, "run-001")
    run.max_attempts = 1
    db.commit()
    db.close()
    manager = MagicMock()
    manager.run_with_streaming.side_effect = RuntimeError("terminal")
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    assert runner.run_agent.fn("run-001", "RUN-001", "test", "/tmp", 5) is None

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


def test_update_missing_task_is_not_silent(worker_db):
    db = worker_db()

    with pytest.raises(Exception, match="Task missing not found"):
        runner._update_task_status(db, "missing", "failed", error="error")

    db.close()
