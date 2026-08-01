import pytest

from app.db.models import Agent, AgentRun, Task
from app.services.agent_matcher import AgentMatcher
from app.services.agent_suggester import AgentSuggester


def test_suggested_agents_are_ranked_by_skills_and_performance(db_session):
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
    task = Task(
        id="MATCH-001",
        project="frontend",
        title="Build React TypeScript dashboard",
        tags=["frontend", "react"],
    )
    db_session.add(task)
    db_session.commit()

    suggestions = AgentMatcher(db_session).suggest_agents(task)

    assert [item.agent_id for item in suggestions] == [
        "@frontend-agent",
        "@backend-agent",
    ]
    assert suggestions[0].score > suggestions[1].score


def test_suggested_agents_consider_similar_run_outcomes(db_session):
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

    suggestions = AgentMatcher(db_session).suggest_agents(current)

    assert suggestions[0].agent_id == good.id


def test_agent_suggester_executor_role_has_no_capability_filter(db_session):
    db_session.add(
        Agent(
            id="@plain-executor",
            name="Plain Executor",
            role="executor",
            capabilities=["python"],
            status="idle",
        )
    )
    task = Task(id="SUGGEST-001", project="backend", title="Python task", tags=["python"])
    db_session.add(task)
    db_session.commit()

    suggestions = AgentSuggester(db_session).suggest(task, role="executor", top_n=3)

    assert [s.agent_id for s in suggestions] == ["@plain-executor"]


def test_agent_suggester_spec_plan_role_filters_by_capability(db_session):
    db_session.add_all(
        [
            Agent(
                id="@coordinator-agent",
                name="Coordinator Agent",
                role="coordinator",
                capabilities=["coordinator"],
                status="idle",
            ),
            Agent(
                id="@plain-executor-2",
                name="Plain Executor",
                role="executor",
                capabilities=["python"],
                status="idle",
            ),
        ]
    )
    task = Task(id="SUGGEST-002", project="backend", title="Spec/plan task")
    db_session.add(task)
    db_session.commit()

    suggestions = AgentSuggester(db_session).suggest(task, role="spec_plan", top_n=3)

    assert [s.agent_id for s in suggestions] == ["@coordinator-agent"]


def test_agent_suggester_returns_empty_when_no_capable_agent(db_session):
    db_session.add(
        Agent(
            id="@plain-executor-3",
            name="Plain Executor",
            role="executor",
            capabilities=["python"],
            status="idle",
        )
    )
    task = Task(id="SUGGEST-003", project="backend", title="Spec/plan task")
    db_session.add(task)
    db_session.commit()

    suggestions = AgentSuggester(db_session).suggest(task, role="spec_plan", top_n=1)

    assert suggestions == []


def test_agent_suggester_rejects_unknown_role(db_session):
    task = Task(id="SUGGEST-004", project="backend", title="Task")
    db_session.add(task)
    db_session.commit()

    with pytest.raises(ValueError):
        AgentSuggester(db_session).suggest(task, role="bogus")


def test_work_type_routing_research(db_session):
    researcher = Agent(
        id="@researcher",
        name="Researcher",
        role="executor",
        capabilities=["research"],
        effort="medium",
        status="idle",
    )
    generalist = Agent(
        id="@generalist",
        name="Generalist",
        role="executor",
        capabilities=["backend"],
        effort="medium",
        status="idle",
    )
    task = Task(
        id="WORK-001",
        project="backend",
        title="Investigate payment failures",
        tags=["research"],
    )
    db_session.add_all([researcher, generalist, task])
    db_session.commit()

    suggestions = AgentMatcher(db_session).suggest_agents(task, top_n=2)

    assert [s.agent_id for s in suggestions] == ["@researcher", "@generalist"]


def test_risk_escalation_high_risk(db_session):
    strong = Agent(
        id="@opus-agent",
        name="Opus Agent",
        role="executor",
        capabilities=["backend"],
        effort="high",
        status="idle",
    )
    weak = Agent(
        id="@flash-agent",
        name="Flash Agent",
        role="executor",
        capabilities=["backend"],
        effort="low",
        status="idle",
    )
    task = Task(
        id="WORK-002",
        project="backend",
        title="Rework payment settlement",
        risk="high",
        tags=["backend"],
    )
    db_session.add_all([strong, weak, task])
    db_session.commit()

    suggestions = AgentMatcher(db_session).suggest_agents(task, top_n=2)

    assert [s.agent_id for s in suggestions] == ["@opus-agent", "@flash-agent"]
    assert suggestions[0].score > suggestions[1].score


def test_four_eyes_exclusion(db_session):
    executor = Agent(
        id="@executor-agent",
        name="Executor Agent",
        role="executor",
        capabilities=["backend"],
        status="idle",
    )
    other = Agent(
        id="@other-agent",
        name="Other Agent",
        role="executor",
        capabilities=["backend"],
        status="idle",
    )
    task = Task(
        id="WORK-003",
        project="backend",
        title="Backend task",
        executor="@executor-agent",
        tags=["backend"],
    )
    db_session.add_all([executor, other, task])
    db_session.commit()

    suggestions = AgentSuggester(db_session).suggest(
        task, role="reviewer", top_n=5, exclude_agent_id=task.executor
    )

    assert [s.agent_id for s in suggestions] == ["@other-agent"]


def test_score_candidates_includes_ineligible_agents_with_rejection_reason(db_session):
    executor = Agent(
        id="@executor-agent-2",
        name="Executor Agent",
        role="executor",
        capabilities=["backend"],
        status="idle",
    )
    offline = Agent(
        id="@offline-agent",
        name="Offline Agent",
        role="executor",
        capabilities=["backend"],
        status="offline",
    )
    task = Task(id="SCORE-001", project="backend", title="Backend task", tags=["backend"])
    db_session.add_all([executor, offline, task])
    db_session.commit()

    result = AgentMatcher(db_session).score_candidates(task, top_n=3)

    by_id = {c.agent_id: c for c in result.candidates}
    assert by_id["@executor-agent-2"].eligible is True
    assert by_id["@executor-agent-2"].final_score is not None
    assert by_id["@offline-agent"].eligible is False
    assert by_id["@offline-agent"].rejection_reason == "agent status is offline"
    assert [s.agent_id for s in result.suggestions] == ["@executor-agent-2"]
    assert result.feature_snapshot["task_id"] == "SCORE-001"


def test_suggest_agents_matches_score_candidates_eligible_ranking(db_session):
    agent = Agent(
        id="@sync-agent",
        name="Sync Agent",
        role="executor",
        capabilities=["backend"],
        status="idle",
    )
    task = Task(id="SCORE-002", project="backend", title="Backend task", tags=["backend"])
    db_session.add_all([agent, task])
    db_session.commit()

    matcher = AgentMatcher(db_session)
    suggestions = matcher.suggest_agents(task, top_n=3)
    scoring = matcher.score_candidates(task, top_n=3)

    assert suggestions == scoring.suggestions
