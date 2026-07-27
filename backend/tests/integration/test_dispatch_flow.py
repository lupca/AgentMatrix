from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
from app.db.models import Agent, AgentRun, Project, Task
from app.main import app


@pytest.fixture
def dispatch_context():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="test", name="Test", repo_root="/tmp"))
    db.add(
        Task(
            id="T-INT-001",
            project="test",
            title="Test task",
            status="todo",
            mode="bypass",
            acceptance_criteria=["Tests pass"],
        )
    )
    db.add(
        Agent(
            id="@test-agent",
            name="Test Agent",
            role="executor",
            cli="codex",
        )
    )
    db.commit()

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client, db
    app.dependency_overrides.clear()
    db.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_dispatch_creates_run_and_queues(dispatch_context):
    client, db = dispatch_context
    message = MagicMock(message_id="message-123")
    with patch("app.api.dispatch.run_agent") as actor:
        actor.send.return_value = message
        response = client.post(
            "/api/dispatch",
            json={"task_id": "T-INT-001", "agent_id": "@test-agent"},
        )

    assert response.status_code == 200
    data = response.json()
    run = db.get(AgentRun, data["run_id"])
    assert data["status"] == "queued"
    assert run.status == "queued"
    assert run.dramatiq_message_id == "message-123"
    assert run.timeout_seconds == 900
    assert db.get(Task, "T-INT-001").status == "dispatched"
    actor.send.assert_called_once()


def test_dispatch_rejects_missing_task(dispatch_context):
    client, _ = dispatch_context
    response = client.post(
        "/api/dispatch",
        json={"task_id": "MISSING", "agent_id": "@test-agent"},
    )

    assert response.status_code == 404


def test_dispatch_rejects_missing_agent(dispatch_context):
    client, _ = dispatch_context
    response = client.post(
        "/api/dispatch",
        json={"task_id": "T-INT-001", "agent_id": "@missing"},
    )

    assert response.status_code == 404


def test_dispatch_rejects_project_without_repo_root(dispatch_context):
    client, db = dispatch_context
    db.get(Project, "test").repo_root = None
    db.commit()

    response = client.post(
        "/api/dispatch",
        json={"task_id": "T-INT-001", "agent_id": "@test-agent"},
    )

    assert response.status_code == 422
    assert "repo_root" in response.json()["detail"]


def test_dispatch_rejects_duplicate_active_run(dispatch_context):
    client, db = dispatch_context
    db.add(
        AgentRun(
            id="existing",
            task_id="T-INT-001",
            agent_id="@test-agent",
            cli="codex",
            command="echo test",
            status="running",
        )
    )
    db.commit()

    response = client.post(
        "/api/dispatch",
        json={"task_id": "T-INT-001", "agent_id": "@test-agent"},
    )

    assert response.status_code == 409


def test_get_run_status(dispatch_context):
    client, db = dispatch_context
    db.add(
        AgentRun(
            id="run-status",
            task_id="T-INT-001",
            agent_id="@test-agent",
            cli="codex",
            command="echo test",
            status="running",
            pid=12345,
        )
    )
    db.commit()

    response = client.get("/api/dispatch/run-status")

    assert response.status_code == 200
    assert response.json()["status"] == "running"
    assert response.json()["pid"] == 12345


def test_get_missing_run_status(dispatch_context):
    client, _ = dispatch_context

    assert client.get("/api/dispatch/missing").status_code == 404


def test_cancel_signals_worker_and_updates_run(dispatch_context):
    client, db = dispatch_context
    db.add(
        AgentRun(
            id="run-cancel",
            task_id="T-INT-001",
            agent_id="@test-agent",
            cli="codex",
            command="sleep 60",
            status="running",
        )
    )
    db.commit()

    with (
        patch("app.api.dispatch.request_cancel") as signal_cancel,
        patch("app.api.dispatch.publish_status") as status_event,
    ):
        response = client.post("/api/dispatch/run-cancel/cancel")

    db.expire_all()
    assert response.status_code == 200
    assert db.get(AgentRun, "run-cancel").status == "cancelled"
    signal_cancel.assert_called_once()
    status_event.assert_called_once()


def test_broker_failure_is_recorded(dispatch_context):
    client, db = dispatch_context
    with patch("app.api.dispatch.run_agent") as actor:
        actor.send.side_effect = RuntimeError("redis unavailable")
        response = client.post(
            "/api/dispatch",
            json={"task_id": "T-INT-001", "agent_id": "@test-agent"},
        )

    run = db.query(AgentRun).one()
    assert response.status_code == 503
    assert run.status == "failed"
    assert "redis unavailable" in run.error_message


def test_cancel_rejects_terminal_or_missing_run(dispatch_context):
    client, db = dispatch_context
    db.add(
        AgentRun(
            id="run-done",
            task_id="T-INT-001",
            agent_id="@test-agent",
            cli="codex",
            command="echo test",
            status="success",
        )
    )
    db.commit()

    assert client.post("/api/dispatch/missing/cancel").status_code == 404
    assert client.post("/api/dispatch/run-done/cancel").status_code == 400


def test_cancel_control_channel_failure_returns_503(dispatch_context):
    client, db = dispatch_context
    db.add(
        AgentRun(
            id="run-control-error",
            task_id="T-INT-001",
            agent_id="@test-agent",
            cli="codex",
            command="sleep 60",
            status="running",
        )
    )
    db.commit()

    with patch(
        "app.api.dispatch.request_cancel",
        side_effect=RuntimeError("redis unavailable"),
    ):
        response = client.post("/api/dispatch/run-control-error/cancel")

    assert response.status_code == 503
    assert db.get(AgentRun, "run-control-error").status == "running"
