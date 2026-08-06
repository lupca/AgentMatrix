from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.db.models import AgentRun, Task
from app.workers.executor.run_tracker import ExecutionTracker


def test_execution_tracker_record_heartbeat_updates_timestamp(db_session):
    task = Task(
        id="TASK-TRACKER-1",
        project="test-project",
        title="Tracker Test Task",
        status="dispatched",
    )
    run = AgentRun(
        id="run-tracker-1",
        task_id="TASK-TRACKER-1",
        agent_id="test-agent",
        cli="claude",
        command="test command",
        status="running",
        attempt=1,
    )
    db_session.add(task)
    db_session.add(run)
    db_session.commit()

    initial_updated_at = run.updated_at
    tracker = ExecutionTracker(db_session, run_id=run.id, task_id=task.id)

    tracker.record_heartbeat(pid=12345)

    db_session.refresh(run)
    assert run.updated_at is not None
    if initial_updated_at is not None:
        assert run.updated_at >= initial_updated_at


def test_execution_tracker_record_heartbeat_handles_db_error():
    db_mock = MagicMock()
    run_mock = MagicMock()
    db_mock.query.return_value.filter.return_value.first.return_value = run_mock
    db_mock.commit.side_effect = RuntimeError("DB error during commit")

    tracker = ExecutionTracker(db_mock, run_id="run-err", task_id="task-err")

    # Should not raise exception
    tracker.record_heartbeat(pid=12345)
    db_mock.rollback.assert_called_once()


def test_execution_tracker_cancel_check_redis_trigger(db_session):
    redis_check = MagicMock(return_value=True)
    tracker = ExecutionTracker(
        db_session,
        run_id="run-1",
        task_id="task-1",
        redis_cancel_check=redis_check,
    )

    assert tracker.cancel_check() is True
    redis_check.assert_called_once()


def test_execution_tracker_cancel_check_agent_run_cancelled(db_session):
    task = Task(
        id="TASK-CANCEL-1",
        project="test-project",
        title="Task",
        status="dispatched",
    )
    run = AgentRun(
        id="run-cancel-1",
        task_id="TASK-CANCEL-1",
        agent_id="test-agent",
        cli="claude",
        command="test command",
        status="cancelled",
        attempt=1,
    )
    db_session.add(task)
    db_session.add(run)
    db_session.commit()

    redis_check = MagicMock(return_value=False)
    tracker = ExecutionTracker(
        db_session,
        run_id=run.id,
        task_id=task.id,
        redis_cancel_check=redis_check,
    )

    assert tracker.cancel_check() is True


def test_execution_tracker_cancel_check_task_terminal_statuses(db_session):
    for terminal_status in ["done", "failed", "cancelled"]:
        kwargs = {}
        if terminal_status == "done":
            kwargs = {
                "executor": "exec-1",
                "reviewer": "rev-1",
                "result_ref": "base..head",
            }
        task = Task(
            id=f"TASK-TERM-{terminal_status}",
            project="test-project",
            title="Task",
            status=terminal_status,
            **kwargs,
        )
        run = AgentRun(
            id=f"run-term-{terminal_status}",
            task_id=task.id,
            agent_id="test-agent",
            cli="claude",
            command="test command",
            status="running",
            attempt=1,
        )
        db_session.add(task)
        db_session.add(run)
        db_session.commit()

        redis_check = MagicMock(return_value=False)
        tracker = ExecutionTracker(
            db_session,
            run_id=run.id,
            task_id=task.id,
            redis_cancel_check=redis_check,
        )

        assert tracker.cancel_check() is True


def test_execution_tracker_cancel_check_not_cancelled(db_session):
    task = Task(
        id="TASK-ACTIVE-1",
        project="test-project",
        title="Active Task",
        status="dispatched",
    )
    run = AgentRun(
        id="run-active-1",
        task_id="TASK-ACTIVE-1",
        agent_id="test-agent",
        cli="claude",
        command="test command",
        status="running",
        attempt=1,
    )
    db_session.add(task)
    db_session.add(run)
    db_session.commit()

    redis_check = MagicMock(return_value=False)
    tracker = ExecutionTracker(
        db_session,
        run_id=run.id,
        task_id=task.id,
        redis_cancel_check=redis_check,
    )

    assert tracker.cancel_check() is False
