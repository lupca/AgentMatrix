from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.workers.agent_runner as runner
from app.db.base import Base
from app.db.models import AgentRun, Project, Task
from app.services.process_manager import ProcessResult, ProcessStatus


@pytest.fixture
def recovery_db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="recovery", name="Recovery", repo_root="/tmp"))
    db.add(
        Task(
            id="T-RECOVERY",
            project="recovery",
            title="Recover",
            status="dispatched",
            executor="@test",
        )
    )
    db.add(
        AgentRun(
            id="recover-run",
            task_id="T-RECOVERY",
            agent_id="@test",
            cli="agy",
            command="echo recovered",
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


def process_manager_for(result):
    manager = MagicMock()
    manager.pid = 100
    manager.run_with_streaming.return_value = iter(result)
    return manager


def test_failed_agent_retries_until_third_attempt_succeeds(recovery_db, monkeypatch):
    managers = [
        process_manager_for([ProcessResult(ProcessStatus.FAILED, 1, "failure 1")]),
        process_manager_for([ProcessResult(ProcessStatus.FAILED, 1, "failure 2")]),
        process_manager_for(
            ["recovered", ProcessResult(ProcessStatus.COMPLETED, 0, None)]
        ),
    ]
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(side_effect=managers))

    with pytest.raises(runner.AgentExecutionError):
        runner.run_agent.fn("recover-run", "T-RECOVERY", "test", "/tmp", 10)
    with pytest.raises(runner.AgentExecutionError):
        runner.run_agent.fn("recover-run", "T-RECOVERY", "test", "/tmp", 10)
    assert runner.run_agent.fn("recover-run", "T-RECOVERY", "test", "/tmp", 10) == 0

    db = recovery_db()
    run = db.get(AgentRun, "recover-run")
    assert run.status == "success"
    assert run.attempt == 3
    assert run.output_lines == 1
    db.close()


def test_timeout_does_not_retry(recovery_db, monkeypatch):
    manager = process_manager_for(
        [ProcessResult(ProcessStatus.TIMEOUT, -1, "Timeout")]
    )
    monkeypatch.setattr(runner, "ProcessManager", MagicMock(return_value=manager))

    runner.run_agent.fn("recover-run", "T-RECOVERY", "sleep 999", "/tmp", 1)

    db = recovery_db()
    run = db.get(AgentRun, "recover-run")
    assert run.status == "timeout"
    assert run.attempt == 1
    db.close()


def test_redelivered_running_run_recovers_after_worker_restart(recovery_db):
    db = recovery_db()
    run = db.get(AgentRun, "recover-run")
    run.status = "running"
    run.started_at = run.queued_at
    run.pid = 999_999
    db.commit()
    db.close()

    runner.run_agent.fn(
        "recover-run",
        "T-RECOVERY",
        "echo recovered",
        "/tmp",
        5,
    )

    db = recovery_db()
    run = db.get(AgentRun, "recover-run")
    assert run.status == "success"
    assert run.attempt == 2
    assert run.pid is None
    db.close()


def test_duplicate_delivery_after_success_is_idempotent(recovery_db, monkeypatch):
    db = recovery_db()
    run = db.get(AgentRun, "recover-run")
    run.status = "success"
    run.exit_code = 0
    db.commit()
    db.close()
    manager = MagicMock()
    monkeypatch.setattr(runner, "ProcessManager", manager)

    assert (
        runner.run_agent.fn(
            "recover-run",
            "T-RECOVERY",
            "echo should-not-run",
            "/tmp",
            5,
        )
        == 0
    )
    manager.return_value.run_with_streaming.assert_not_called()
