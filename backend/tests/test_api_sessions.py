def test_create_and_get_task_session(client):
    # Create task first
    task_res = client.post("/api/tasks", json={"project": "web", "title": "Session task"})
    task_id = task_res.json()["id"]

    session_payload = {
        "context_level": "task",
        "task_id": task_id,
        "thread_id": "thread-123",
        "current_gate": "dispatch",
        "messages": [{"role": "user", "content": "Hello"}]
    }

    create_res = client.post("/api/sessions", json=session_payload)
    assert create_res.status_code == 201
    session_data = create_res.json()
    assert session_data["task_id"] == task_id
    assert session_data["project_id"] == "web"
    assert session_data["context_level"] == "task"
    assert session_data["status"] == "active"
    assert session_data["message_count"] == 1
    assert session_data["thread_id"] == "thread-123"
    assert session_data["current_gate"] == "dispatch"
    assert len(session_data["messages"]) == 1

    session_id = session_data["id"]

    # Get session by id
    get_res = client.get(f"/api/sessions/{session_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == session_id


def test_create_project_session(client):
    res = client.post(
        "/api/sessions",
        json={"context_level": "project", "project_id": "control-tower-v2", "title": "Planning"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["context_level"] == "project"
    assert data["project_id"] == "control-tower-v2"
    assert data["task_id"] is None
    assert data["title"] == "Planning"


def test_create_global_session_defaults(client):
    res = client.post("/api/sessions", json={"thread_id": "t-global"})
    assert res.status_code == 201
    data = res.json()
    assert data["context_level"] == "global"
    assert data["project_id"] is None
    assert data["task_id"] is None


def test_create_project_session_requires_project_id(client):
    res = client.post("/api/sessions", json={"context_level": "project"})
    assert res.status_code == 422


def test_create_task_session_requires_task_id(client):
    res = client.post(
        "/api/sessions",
        json={"context_level": "task", "project_id": "control-tower-v2"},
    )
    assert res.status_code == 422


def test_create_global_session_rejects_project_id(client):
    res = client.post(
        "/api/sessions",
        json={"context_level": "global", "project_id": "control-tower-v2"},
    )
    assert res.status_code == 422


def test_create_session_unknown_task_404(client):
    res = client.post(
        "/api/sessions",
        json={"context_level": "task", "task_id": "NOPE-1"},
    )
    assert res.status_code == 404


def test_list_sessions_filter_and_pagination(client):
    client.post("/api/sessions", json={"thread_id": "t1", "current_gate": "g1"})
    client.post("/api/sessions", json={"thread_id": "t2", "current_gate": "g2"})
    client.post(
        "/api/sessions",
        json={"context_level": "project", "project_id": "control-tower-v2"},
    )

    res = client.get("/api/sessions")
    assert res.status_code == 200
    sessions = res.json()
    assert len(sessions) >= 3

    # Pagination
    res_page = client.get("/api/sessions?limit=1&offset=0")
    assert res_page.status_code == 200
    assert len(res_page.json()) == 1

    # Filter by context_level
    res_project = client.get("/api/sessions?context_level=project")
    assert res_project.status_code == 200
    assert all(s["context_level"] == "project" for s in res_project.json())

    # Filter by project_id
    res_by_project = client.get("/api/sessions?project_id=control-tower-v2")
    assert res_by_project.status_code == 200
    assert all(s["project_id"] == "control-tower-v2" for s in res_by_project.json())

    # Filter by status
    res_active = client.get("/api/sessions?status=active")
    assert res_active.status_code == 200
    assert all(s["status"] == "active" for s in res_active.json())


def test_patch_session(client):
    s = client.post("/api/sessions", json={"thread_id": "t1", "current_gate": "gate_1"}).json()
    session_id = s["id"]

    patch_res = client.patch(f"/api/sessions/{session_id}", json={
        "current_gate": "gate_2",
        "messages": [{"role": "system", "content": "Updated"}]
    })
    assert patch_res.status_code == 200
    updated = patch_res.json()
    assert updated["current_gate"] == "gate_2"
    assert len(updated["messages"]) == 1
    assert updated["messages"][0]["content"] == "Updated"
    assert updated["message_count"] == 1


def test_patch_session_title_status_pinned(client):
    s = client.post("/api/sessions", json={"thread_id": "t1"}).json()
    session_id = s["id"]

    patch_res = client.patch(
        f"/api/sessions/{session_id}",
        json={"title": "Renamed", "status": "archived", "pinned": True},
    )
    assert patch_res.status_code == 200
    updated = patch_res.json()
    assert updated["title"] == "Renamed"
    assert updated["status"] == "archived"
    assert updated["pinned"] is True


def test_session_model_selection_is_persisted_and_validated(client):
    session = client.post(
        "/api/sessions",
        json={
            "thread_id": "model-session",
            "selected_model": "claude-sonnet-4",
        },
    )

    assert session.status_code == 201
    assert session.json()["selected_provider"] == "anthropic"
    session_id = session.json()["id"]

    switched = client.patch(
        f"/api/sessions/{session_id}",
        json={
            "selected_provider": "google",
            "selected_model": "gemini-2.5-flash",
        },
    )
    assert switched.status_code == 200
    assert switched.json()["selected_provider"] == "google"
    assert switched.json()["selected_model"] == "gemini-2.5-flash"

    invalid = client.patch(
        f"/api/sessions/{session_id}",
        json={
            "selected_provider": "anthropic",
            "selected_model": "gemini-2.5-flash",
        },
    )
    assert invalid.status_code == 422


def test_get_session_404(client):
    res = client.get("/api/sessions/nonexistent-id")
    assert res.status_code == 404
