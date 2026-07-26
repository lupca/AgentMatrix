def test_stats_overview(client):
    client.post("/api/tasks", json={"id": "CT-101", "project": "p1", "title": "Task 1", "status": "todo"})
    client.post("/api/tasks", json={"id": "CT-102", "project": "p1", "title": "Task 2", "status": "in_progress"})
    client.post("/api/tasks", json={"id": "CT-103", "project": "p2", "title": "Task 3", "status": "done"})
    client.post("/api/tasks", json={"id": "CT-104", "project": "p2", "title": "Task 4", "status": "done"})

    res = client.get("/api/stats/overview")
    assert res.status_code == 200
    data = res.json()
    assert data == {
        "totalTasks": 4,
        "completedTasks": 2,
        "activeGates": 2,
        "tasksByStatus": {"todo": 1, "in_progress": 1, "done": 2},
    }


def test_stats_projects(client):
    client.post("/api/projects", json={"id": "p1", "name": "Project One"})
    client.post("/api/projects", json={"id": "p2", "name": "Project Two"})

    client.post("/api/tasks", json={"id": "CT-201", "project": "p1", "title": "Task 1", "status": "todo"})
    client.post("/api/tasks", json={"id": "CT-202", "project": "p1", "title": "Task 2", "status": "done"})
    client.post("/api/tasks", json={"id": "CT-203", "project": "p2", "title": "Task 3", "status": "in_progress"})

    res = client.get("/api/stats/projects")
    assert res.status_code == 200
    data = res.json()

    p1_stats = next(item for item in data if item["project_id"] == "p1")
    assert p1_stats["project_name"] == "Project One"
    assert p1_stats["total_tasks"] == 2
    assert p1_stats["done_tasks"] == 1
    assert p1_stats["active_tasks"] == 1
    assert p1_stats["by_status"] == {"todo": 1, "done": 1}

    p2_stats = next(item for item in data if item["project_id"] == "p2")
    assert p2_stats["project_name"] == "Project Two"
    assert p2_stats["total_tasks"] == 1
    assert p2_stats["done_tasks"] == 0
    assert p2_stats["active_tasks"] == 1


def test_stats_agents(client):
    client.post("/api/agents", json={"id": "agent-exec", "name": "Executor Agent", "role": "developer"})
    client.post("/api/agents", json={"id": "agent-rev", "name": "Reviewer Agent", "role": "reviewer"})

    client.post("/api/tasks", json={
        "id": "CT-301",
        "project": "p1",
        "title": "Task 1",
        "status": "done",
        "executor": "agent-exec",
        "reviewer": "agent-rev"
    })
    client.post("/api/tasks", json={
        "id": "CT-302",
        "project": "p1",
        "title": "Task 2",
        "status": "in_progress",
        "executor": "agent-exec",
        "reviewer": "agent-rev"
    })

    res = client.get("/api/stats/agents")
    assert res.status_code == 200
    data = res.json()

    exec_stats = next(item for item in data if item["agent_id"] == "agent-exec")
    assert exec_stats["tasks_executed"] == 2
    assert exec_stats["tasks_completed"] == 1
    assert exec_stats["active_tasks"] == 1
    assert exec_stats["success_rate"] == 0.5

    rev_stats = next(item for item in data if item["agent_id"] == "agent-rev")
    assert rev_stats["tasks_reviewed"] == 2
