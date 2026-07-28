from datetime import datetime, timedelta, timezone
from app.db.models import Project as ProjectModel, Task as TaskModel
from app.services.task_event_service import emit_task_event


def test_poll_events_empty(client):
    res = client.get("/api/events")
    assert res.status_code == 200
    data = res.json()
    assert data["events"] == []
    assert "cursor" in data
    assert data["has_more"] is False


def test_poll_events_with_filters(client, db_session):
    proj = ProjectModel(id="web", name="Web Project")
    db_session.add(proj)
    task1 = TaskModel(id="TASK-101", project="web", title="Task 101")
    task2 = TaskModel(id="TASK-102", project="web", title="Task 102")
    db_session.add_all([task1, task2])
    db_session.commit()

    emit_task_event("TASK-101", "dispatched", {"run_id": "run-1"}, db=db_session)
    emit_task_event("TASK-101", "done", {"run_id": "run-1", "result_ref": "abc"}, db=db_session)
    emit_task_event("TASK-102", "failed", {"run_id": "run-2", "error": "failed"}, db=db_session)

    # All events
    res = client.get("/api/events")
    assert res.status_code == 200
    data = res.json()
    assert len(data["events"]) == 3

    # Filter by task_id
    res_task = client.get("/api/events?task_id=TASK-101")
    assert res_task.status_code == 200
    events_101 = res_task.json()["events"]
    assert len(events_101) == 2
    assert {e["event_type"] for e in events_101} == {"dispatched", "done"}

    # Filter by event types
    res_types = client.get("/api/events?types=done,failed")
    assert res_types.status_code == 200
    events_types = res_types.json()["events"]
    assert len(events_types) == 2
    assert {e["event_type"] for e in events_types} == {"done", "failed"}

    # Filter by since
    past_cursor = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    res_since = client.get(f"/api/events?since={past_cursor}")
    assert res_since.status_code == 200
    assert len(res_since.json()["events"]) == 3


def test_poll_events_pagination(client, db_session):
    proj = ProjectModel(id="web", name="Web Project")
    db_session.add(proj)
    task = TaskModel(id="TASK-201", project="web", title="Task 201")
    db_session.add(task)
    db_session.commit()

    base_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=10)
    for i in range(5):
        evt = emit_task_event("TASK-201", "dispatched", {"index": i}, db=db_session)
        evt.created_at = base_time + timedelta(seconds=i)
        db_session.commit()

    res = client.get("/api/events?limit=3")
    assert res.status_code == 200
    data = res.json()
    assert len(data["events"]) == 3
    assert data["has_more"] is True
    assert "cursor" in data

    # Poll next page using returning cursor
    next_cursor = data["cursor"]
    res_next = client.get(f"/api/events?since={next_cursor}&limit=3")
    assert res_next.status_code == 200
    data_next = res_next.json()
    assert len(data_next["events"]) == 2
    assert data_next["has_more"] is False


def test_poll_events_invalid_since(client):
    res = client.get("/api/events?since=invalid-date")
    assert res.status_code == 400
    assert "Invalid 'since' timestamp format" in res.json()["detail"]
