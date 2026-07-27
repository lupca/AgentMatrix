def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_task_auto_id(client):
    payload = {
        "project": "web",
        "title": "Build landing page",
        "priority": "high"
    }
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == "WEB-001"
    assert data["project"] == "web"
    assert data["title"] == "Build landing page"
    assert data["status"] == "todo"
    assert data["priority"] == "high"


def test_create_task_explicit_id(client):
    payload = {
        "id": "CTV2-002",
        "project": "control-tower-v2",
        "title": "FastAPI CRUD"
    }
    response = client.post("/api/tasks", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == "CTV2-002"
    assert data["project"] == "control-tower-v2"


def test_create_duplicate_task_id_fails(client):
    payload = {
        "id": "CTV2-002",
        "project": "control-tower-v2",
        "title": "FastAPI CRUD"
    }
    response1 = client.post("/api/tasks", json=payload)
    assert response1.status_code == 201

    response2 = client.post("/api/tasks", json=payload)
    assert response2.status_code == 400
    assert "already exists" in response2.json()["detail"]


def test_get_tasks_with_filtering_and_pagination(client, db_session):
    first = client.post("/api/tasks", json={"project": "web", "title": "Task 1"})
    second = client.post("/api/tasks", json={"project": "web", "title": "Task 2"})
    client.post("/api/tasks", json={"project": "backend", "title": "Task 3"})
    from app.db.models import Task
    db_session.get(Task, second.json()["id"]).status = "in_progress"
    db_session.commit()

    # Filter by status
    res = client.get("/api/tasks?status=todo")
    assert res.status_code == 200
    tasks = res.json()
    assert len(tasks) == 2

    # Filter by project
    res = client.get("/api/tasks?project=web")
    assert res.status_code == 200
    tasks = res.json()
    assert len(tasks) == 2

    # Filter by project and status
    res = client.get("/api/tasks?project=web&status=todo")
    assert res.status_code == 200
    tasks = res.json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Task 1"

    # Pagination
    res = client.get("/api/tasks?limit=1&offset=1")
    assert res.status_code == 200
    tasks = res.json()
    assert len(tasks) == 1


def test_get_task_by_id(client):
    create_res = client.post("/api/tasks", json={"project": "web", "title": "Find me"})
    task_id = create_res.json()["id"]

    res = client.get(f"/api/tasks/{task_id}")
    assert res.status_code == 200
    assert res.json()["title"] == "Find me"

    res_404 = client.get("/api/tasks/NONEXISTENT")
    assert res_404.status_code == 404


def test_patch_task_and_audit_log(client):
    create_res = client.post("/api/tasks", json={
        "project": "web",
        "title": "Original Title"
    })
    task_id = create_res.json()["id"]

    protected_res = client.patch(f"/api/tasks/{task_id}", json={
        "status": "in_review",
    })
    assert protected_res.status_code == 422

    patch_res = client.patch(f"/api/tasks/{task_id}", json={
        "plan": "Step 1, Step 2",
        "acceptance_criteria": ["AC1", "AC2"]
    })
    assert patch_res.status_code == 200
    updated_task = patch_res.json()
    assert updated_task["status"] == "todo"
    assert updated_task["plan"] == "Step 1, Step 2"
    assert updated_task["acceptance_criteria"] == ["AC1", "AC2"]

    # History endpoint check
    hist_res = client.get(f"/api/tasks/{task_id}/history")
    assert hist_res.status_code == 200
    history = hist_res.json()
    assert len(history) == 2  # create_task + update_task
    assert history[0]["action"] == "create_task"
    assert history[1]["action"] == "update_task"


def test_audit_logs_endpoint(client):
    create_res = client.post("/api/tasks", json={"project": "web", "title": "Task with audit"})
    task_id = create_res.json()["id"]

    res = client.get(f"/api/audit?task_id={task_id}")
    assert res.status_code == 200
    logs = res.json()
    assert len(logs) == 1
    assert logs[0]["task_id"] == task_id
    assert logs[0]["action"] == "create_task"


def test_create_task_with_depends_on_records_edges(client):
    upstream = client.post("/api/tasks", json={"project": "web", "title": "Upstream"})
    upstream_id = upstream.json()["id"]

    downstream = client.post(
        "/api/tasks",
        json={
            "project": "web",
            "title": "Downstream",
            "depends_on": [upstream_id],
        },
    )
    assert downstream.status_code == 201
    data = downstream.json()
    assert data["depends_on"] == [upstream_id]

    fetched = client.get(f"/api/tasks/{data['id']}")
    assert fetched.json()["depends_on"] == [upstream_id]


def test_create_task_with_missing_dependency_is_rejected(client):
    response = client.post(
        "/api/tasks",
        json={
            "project": "web",
            "title": "Orphan dependent",
            "depends_on": ["MISSING-TASK"],
        },
    )
    assert response.status_code == 422
