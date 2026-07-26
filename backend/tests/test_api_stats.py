from app.db.models import GateRecord, Task


def _persist_task(
    db,
    task_id,
    project,
    title,
    status,
    *,
    executor=None,
    reviewer=None,
):
    if status == "done":
        executor = executor or f"{task_id}-executor"
        reviewer = reviewer or f"{task_id}-reviewer"
    task = Task(
        id=task_id,
        project=project,
        title=title,
        status=status,
        executor=executor,
        reviewer=reviewer,
        result_ref=f"{task_id}-result" if status == "done" else None,
        verdict="pass" if status == "done" else None,
    )
    db.add(task)
    if status == "done":
        db.add(
            GateRecord(
                task_id=task_id,
                gate_type="verdict",
                status="approved",
                actor=reviewer,
                mode="bypass",
                output_ref="pass",
            )
        )
    db.commit()


def test_stats_overview(client, db_session):
    _persist_task(db_session, "CT-101", "p1", "Task 1", "todo")
    _persist_task(db_session, "CT-102", "p1", "Task 2", "in_progress")
    _persist_task(db_session, "CT-103", "p2", "Task 3", "done")
    _persist_task(db_session, "CT-104", "p2", "Task 4", "done")

    res = client.get("/api/stats/overview")
    assert res.status_code == 200
    data = res.json()
    assert data == {
        "totalTasks": 4,
        "completedTasks": 2,
        "activeGates": 2,
        "tasksByStatus": {"todo": 1, "in_progress": 1, "done": 2},
    }


def test_stats_projects(client, db_session):
    client.post("/api/projects", json={"id": "p1", "name": "Project One"})
    client.post("/api/projects", json={"id": "p2", "name": "Project Two"})

    _persist_task(db_session, "CT-201", "p1", "Task 1", "todo")
    _persist_task(db_session, "CT-202", "p1", "Task 2", "done")
    _persist_task(db_session, "CT-203", "p2", "Task 3", "in_progress")

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


def test_stats_agents(client, db_session):
    client.post("/api/agents", json={"id": "agent-exec", "name": "Executor Agent", "role": "developer"})
    client.post("/api/agents", json={"id": "agent-rev", "name": "Reviewer Agent", "role": "reviewer"})

    _persist_task(
        db_session,
        "CT-301",
        "p1",
        "Task 1",
        "done",
        executor="agent-exec",
        reviewer="agent-rev",
    )
    _persist_task(
        db_session,
        "CT-302",
        "p1",
        "Task 2",
        "in_progress",
        executor="agent-exec",
        reviewer="agent-rev",
    )

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
