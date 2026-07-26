import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
from app.db.models import AgentOutputChunk, AgentRun, Project, Task
from app.main import app


def parse_events(body):
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.fixture
def streaming_context():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add(Project(id="stream", name="Stream", repo_root="/tmp"))
    db.add(Task(id="T-STREAM", project="stream", title="Streaming"))
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


def add_run(db, *, run_id="run-stream", status="success"):
    run = AgentRun(
        id=run_id,
        task_id="T-STREAM",
        agent_id="@test",
        cli="agy",
        command="echo test",
        status=status,
        exit_code=0 if status == "success" else None,
        output_lines=3,
        output_bytes=15,
    )
    db.add(run)
    db.add(
        AgentOutputChunk(
            run_id=run_id,
            chunk_index=0,
            content="line1\nline2\nline3",
        )
    )
    db.commit()
    return run


def test_completed_stream_replays_history_status_and_done(streaming_context):
    client, db = streaming_context
    add_run(db)

    response = client.get("/api/runs/run-stream/stream")
    events = parse_events(response.text)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [event["content"] for event in events if event["type"] == "history"] == [
        "line1",
        "line2",
        "line3",
    ]
    assert events[-2]["status"] == "success"
    assert events[-1]["type"] == "done"


def test_stream_reconnection_skips_acknowledged_history(streaming_context):
    client, db = streaming_context
    add_run(db)

    response = client.get(
        "/api/runs/run-stream/stream",
        headers={"Last-Event-ID": "2"},
    )
    events = parse_events(response.text)

    history = [event for event in events if event["type"] == "history"]
    assert history == [{"type": "history", "content": "line3", "index": 3}]
    assert events[-1]["type"] == "done"


def test_stream_accepts_query_resume_id_and_invalid_header(streaming_context):
    client, db = streaming_context
    add_run(db)

    query_response = client.get("/api/runs/run-stream/stream?last_event_id=1")
    header_response = client.get(
        "/api/runs/run-stream/stream",
        headers={"Last-Event-ID": "not-a-number"},
    )

    query_history = [
        event for event in parse_events(query_response.text) if event["type"] == "history"
    ]
    header_history = [
        event for event in parse_events(header_response.text) if event["type"] == "history"
    ]
    assert [event["index"] for event in query_history] == [2, 3]
    assert [event["index"] for event in header_history] == [1, 2, 3]


class FakePubSub:
    def __init__(self, messages=None):
        self.messages = iter(
            messages
            or [
                {
                    "data": json.dumps(
                        {"type": "stdout", "content": "live1", "index": 1}
                    )
                },
                {
                    "data": json.dumps(
                        {"type": "stdout", "content": "live2", "index": 2}
                    )
                },
                {"data": json.dumps({"type": "status", "status": "success"})},
            ]
        )
        self.subscribed = None

    async def subscribe(self, channel):
        self.subscribed = channel

    async def get_message(self, **kwargs):
        return next(self.messages)

    async def unsubscribe(self, channel):
        self.subscribed = None

    async def aclose(self):
        pass


class FakeRedis:
    def __init__(self, messages=None):
        self.pubsub_instance = FakePubSub(messages)

    def pubsub(self):
        return self.pubsub_instance

    async def aclose(self):
        pass


def test_running_stream_forwards_live_pubsub_events(streaming_context):
    client, db = streaming_context
    add_run(db, run_id="run-live", status="running")
    db.query(AgentOutputChunk).filter(AgentOutputChunk.run_id == "run-live").delete()
    db.commit()

    fake_redis = FakeRedis()

    async def redis_factory():
        return fake_redis

    with patch("app.api.stream.create_redis_client", redis_factory):
        response = client.get("/api/runs/run-live/stream")

    events = parse_events(response.text)
    assert [event.get("content") for event in events[:2]] == ["live1", "live2"]
    assert events[2]["status"] == "success"
    assert events[3]["type"] == "done"
    assert fake_redis.pubsub_instance.subscribed is None


def test_running_stream_combines_history_and_live_without_duplicates(streaming_context):
    client, db = streaming_context
    add_run(db, run_id="run-race", status="running")
    fake_redis = FakeRedis(
        [
            {
                "data": json.dumps(
                    {"type": "stdout", "content": "line3", "index": 3}
                )
            },
            {
                "data": json.dumps(
                    {"type": "stdout", "content": "line4", "index": 4}
                )
            },
            {"data": json.dumps({"type": "status", "status": "success"})},
        ]
    )

    async def redis_factory():
        return fake_redis

    with patch("app.api.stream.create_redis_client", redis_factory):
        response = client.get("/api/runs/run-race/stream")

    events = parse_events(response.text)
    output = [
        event["content"]
        for event in events
        if event["type"] in {"history", "stdout"}
    ]
    assert output == ["line1", "line2", "line3", "line4"]


def test_running_stream_emits_heartbeat_and_handles_bytes(streaming_context):
    client, db = streaming_context
    add_run(db, run_id="run-heartbeat", status="running")
    db.query(AgentOutputChunk).filter(
        AgentOutputChunk.run_id == "run-heartbeat"
    ).delete()
    db.commit()
    fake_redis = FakeRedis(
        [
            None,
            {"data": b"not-json"},
            {
                "data": json.dumps(
                    {"type": "stdout", "content": "after-heartbeat", "index": 1}
                ).encode()
            },
            {"data": json.dumps({"type": "status", "status": "success"})},
        ]
    )

    async def redis_factory():
        return fake_redis

    with patch("app.api.stream.create_redis_client", redis_factory):
        response = client.get("/api/runs/run-heartbeat/stream")

    assert ": heartbeat" in response.text
    events = parse_events(response.text)
    assert events[0]["content"] == "after-heartbeat"
    assert events[-1]["type"] == "done"


def test_stream_reports_redis_connection_error(streaming_context):
    client, db = streaming_context
    add_run(db, run_id="run-redis-error", status="running")

    class BrokenPubSub(FakePubSub):
        async def subscribe(self, channel):
            from app.api import stream as stream_api

            raise stream_api.aioredis.ConnectionError("redis unavailable")

    class BrokenRedis(FakeRedis):
        def __init__(self):
            self.pubsub_instance = BrokenPubSub()

    async def redis_factory():
        return BrokenRedis()

    with patch("app.api.stream.create_redis_client", redis_factory):
        response = client.get("/api/runs/run-redis-error/stream")

    assert parse_events(response.text)[0]["error"] == "stream_unavailable"


def test_get_full_output(streaming_context):
    client, db = streaming_context
    add_run(db)

    response = client.get("/api/runs/run-stream/output")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-stream",
        "status": "success",
        "output": "line1\nline2\nline3",
        "line_count": 3,
        "byte_count": 15,
    }


def test_missing_run_returns_404(streaming_context):
    client, _ = streaming_context

    assert client.get("/api/runs/missing/stream").status_code == 404
    assert client.get("/api/runs/missing/output").status_code == 404
