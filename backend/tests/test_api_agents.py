def test_create_agent(client):
    payload = {
        "id": "agent-007",
        "name": "James",
        "role": "executor",
        "capabilities": ["python", "fastapi"],
        "status": "idle"
    }
    response = client.post("/api/agents", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["id"] == "agent-007"
    assert data["name"] == "James"
    assert data["role"] == "executor"
    assert data["capabilities"] == ["python", "fastapi"]
    assert data["status"] == "idle"


def test_create_duplicate_agent_id_fails(client):
    payload = {
        "id": "agent-dup",
        "name": "Duplicate Agent",
        "role": "reviewer"
    }
    res1 = client.post("/api/agents", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/api/agents", json=payload)
    assert res2.status_code == 400
    assert "already exists" in res2.json()["detail"]


def test_get_agents_filtering_and_pagination(client):
    client.post("/api/agents", json={"id": "a1", "name": "Agent 1", "role": "executor", "status": "idle"})
    client.post("/api/agents", json={"id": "a2", "name": "Agent 2", "role": "reviewer", "status": "busy"})
    client.post("/api/agents", json={"id": "a3", "name": "Agent 3", "role": "executor", "status": "busy"})

    res_role = client.get("/api/agents?role=executor")
    assert res_role.status_code == 200
    assert len(res_role.json()) == 2

    res_status = client.get("/api/agents?status=busy")
    assert res_status.status_code == 200
    assert len(res_status.json()) == 2

    res_both = client.get("/api/agents?role=executor&status=busy")
    assert res_both.status_code == 200
    assert len(res_both.json()) == 1


def test_get_agent_by_id(client):
    client.post("/api/agents", json={"id": "agent-find", "name": "Agent Find", "role": "planner"})

    res = client.get("/api/agents/agent-find")
    assert res.status_code == 200
    assert res.json()["name"] == "Agent Find"

    res_404 = client.get("/api/agents/nonexistent")
    assert res_404.status_code == 404


def test_patch_agent(client):
    client.post("/api/agents", json={"id": "agent-patch", "name": "Original Agent", "role": "planner"})

    res = client.patch("/api/agents/agent-patch", json={"name": "Updated Agent", "status": "busy"})
    assert res.status_code == 200
    assert res.json()["name"] == "Updated Agent"
    assert res.json()["status"] == "busy"


def test_delete_agent(client):
    client.post("/api/agents", json={"id": "agent-del", "name": "To Delete", "role": "test"})

    del_res = client.delete("/api/agents/agent-del")
    assert del_res.status_code == 204

    get_res = client.get("/api/agents/agent-del")
    assert get_res.status_code == 404
