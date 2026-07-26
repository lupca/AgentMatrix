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


def test_coordinator_filter_returns_only_coordinators(client):
    client.post(
        "/api/agents",
        json={
            "id": "coordinator-1",
            "name": "Coordinator",
            "role": "coordinator",
            "model": "claude-sonnet-4-20250514",
            "cli": "claude",
            "is_default": True,
        },
    )
    client.post(
        "/api/agents",
        json={"id": "executor-1", "name": "Executor", "role": "executor"},
    )

    response = client.get("/api/agents?role=coordinator")

    assert response.status_code == 200
    assert [agent["id"] for agent in response.json()] == ["coordinator-1"]
    assert response.json()[0]["is_default"] is True
    assert response.json()[0]["model"] == "claude-sonnet-4-20250514"


def test_setting_default_unsets_other_coordinators(client):
    for agent_id in ("coordinator-a", "coordinator-b"):
        client.post(
            "/api/agents",
            json={
                "id": agent_id,
                "name": agent_id,
                "role": "coordinator",
                "is_default": agent_id.endswith("a"),
            },
        )

    response = client.post("/api/agents/coordinator-b/set-default")

    assert response.status_code == 200
    assert response.json()["is_default"] is True
    coordinators = client.get("/api/agents?role=coordinator").json()
    assert {agent["id"] for agent in coordinators if agent["is_default"]} == {"coordinator-b"}


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
