from app.db.models import Agent, AgentRun, Task


def test_suggested_agents_are_ranked_by_skills_and_performance(client, db_session):
    db_session.add_all(
        [
            Agent(
                id="@frontend-agent",
                name="Frontend Agent",
                role="executor",
                capabilities=["frontend", "react", "typescript"],
                success_rate=0.9,
                status="idle",
            ),
            Agent(
                id="@backend-agent",
                name="Backend Agent",
                role="executor",
                capabilities=["backend", "python"],
                success_rate=0.9,
                status="idle",
            ),
        ]
    )
    db_session.commit()

    task_response = client.post(
        "/api/tasks",
        json={
            "id": "MATCH-001",
            "project": "frontend",
            "title": "Build React TypeScript dashboard",
            "tags": ["frontend", "react"],
        },
    )
    assert task_response.status_code == 201

    response = client.get("/api/tasks/MATCH-001/suggested-agents")

    assert response.status_code == 200
    suggestions = response.json()
    assert [item["agent_id"] for item in suggestions] == [
        "@frontend-agent",
        "@backend-agent",
    ]
    assert suggestions[0]["score"] > suggestions[1]["score"]
    assert "frontend" in suggestions[0]["reason"]


def test_suggested_agents_consider_similar_run_outcomes(client, db_session):
    good = Agent(
        id="@good-agent",
        name="Good Agent",
        role="executor",
        capabilities=["python"],
        success_rate=0.5,
        status="idle",
    )
    weak = Agent(
        id="@weak-agent",
        name="Weak Agent",
        role="executor",
        capabilities=["python"],
        success_rate=0.5,
        status="idle",
    )
    previous_good = Task(id="MATCH-002", project="backend", title="Python API", tags=["python"])
    previous_weak = Task(id="MATCH-003", project="backend", title="Python API", tags=["python"])
    current = Task(id="MATCH-004", project="backend", title="Python API endpoint", tags=["python"])
    db_session.add_all([good, weak, previous_good, previous_weak, current])
    db_session.flush()
    db_session.add_all(
        [
            AgentRun(
                task_id=previous_good.id,
                agent_id=good.id,
                cli="codex",
                command="echo good",
                status="success",
            ),
            AgentRun(
                task_id=previous_weak.id,
                agent_id=weak.id,
                cli="codex",
                command="echo weak",
                status="failed",
            ),
        ]
    )
    db_session.commit()

    response = client.get(f"/api/tasks/{current.id}/suggested-agents")

    assert response.status_code == 200
    assert response.json()[0]["agent_id"] == good.id
