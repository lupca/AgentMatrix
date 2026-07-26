def test_create_and_get_session(client):
    # Create task first
    task_res = client.post("/api/tasks", json={"project": "web", "title": "Session task"})
    task_id = task_res.json()["id"]

    session_payload = {
        "task_id": task_id,
        "thread_id": "thread-123",
        "current_gate": "dispatch",
        "messages": [{"role": "user", "content": "Hello"}]
    }

    create_res = client.post("/api/sessions", json=session_payload)
    assert create_res.status_code == 201
    session_data = create_res.json()
    assert session_data["task_id"] == task_id
    assert session_data["thread_id"] == "thread-123"
    assert session_data["current_gate"] == "dispatch"
    assert len(session_data["messages"]) == 1

    session_id = session_data["id"]

    # Get session by id
    get_res = client.get(f"/api/sessions/{session_id}")
    assert get_res.status_code == 200
    assert get_res.json()["id"] == session_id


def test_list_sessions_filter_and_pagination(client):
    s1 = client.post("/api/sessions", json={"thread_id": "t1", "current_gate": "g1"}).json()
    s2 = client.post("/api/sessions", json={"thread_id": "t2", "current_gate": "g2"}).json()

    res = client.get("/api/sessions")
    assert res.status_code == 200
    sessions = res.json()
    assert len(sessions) >= 2

    # Pagination
    res_page = client.get("/api/sessions?limit=1&offset=0")
    assert res_page.status_code == 200
    assert len(res_page.json()) == 1


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


def test_get_session_404(client):
    res = client.get("/api/sessions/nonexistent-id")
    assert res.status_code == 404
