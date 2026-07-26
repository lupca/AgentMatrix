from decimal import Decimal

from app.db.models import LLMUsage


def _usage(
    *,
    session_id: str,
    task_id: str,
    operation: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cost_usd: str = "0.001",
    latency_ms: int = 100,
) -> LLMUsage:
    return LLMUsage(
        session_id=session_id,
        task_id=task_id,
        model="gemini-2.5-flash",
        provider="google",
        operation=operation,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd=Decimal(cost_usd),
        latency_ms=latency_ms,
    )


def test_token_stats_aggregate_by_session_task_and_operation(client, db_session):
    db_session.add_all(
        [
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="plan",
                input_tokens=400,
                output_tokens=80,
                cached_tokens=100,
                latency_ms=100,
            ),
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="chat",
                input_tokens=200,
                output_tokens=40,
                latency_ms=200,
            ),
            _usage(
                session_id="session-2",
                task_id="CTV2-002",
                operation="plan",
                input_tokens=400,
                output_tokens=80,
                latency_ms=300,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/stats/tokens")

    assert response.status_code == 200
    data = response.json()
    assert data["total_calls"] == 3
    assert data["total_input_tokens"] == 1_000
    assert data["total_output_tokens"] == 200
    assert data["total_cached_tokens"] == 100
    assert data["total_tokens"] == 1_200
    assert data["total_cost_usd"] == 0.003
    assert data["average_latency_ms"] == 200
    assert len(data["by_session"]) == 2
    assert len(data["by_task"]) == 2

    plan = next(item for item in data["by_operation"] if item["operation"] == "plan")
    assert plan["calls"] == 2
    assert plan["input_tokens"] == 800
    assert plan["total_tokens"] == 960


def test_token_stats_support_operation_filter(client, db_session):
    db_session.add_all(
        [
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="plan",
                input_tokens=250,
                output_tokens=50,
            ),
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="chat",
                input_tokens=500,
                output_tokens=100,
            ),
        ]
    )
    db_session.commit()

    data = client.get("/api/stats/tokens?operation=plan").json()

    assert data["total_calls"] == 1
    assert data["total_input_tokens"] == 250
    assert data["by_operation"][0]["operation"] == "plan"


def test_token_comparison_uses_v1_baseline_per_measured_cycle(client, db_session):
    db_session.add_all(
        [
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="plan",
                input_tokens=400,
                output_tokens=80,
            ),
            _usage(
                session_id="session-1",
                task_id="CTV2-001",
                operation="chat",
                input_tokens=200,
                output_tokens=40,
            ),
            _usage(
                session_id="session-2",
                task_id="CTV2-002",
                operation="plan",
                input_tokens=400,
                output_tokens=80,
            ),
        ]
    )
    db_session.commit()

    response = client.get("/api/stats/tokens/comparison")

    assert response.status_code == 200
    data = response.json()
    assert data["baseline_input_tokens_per_cycle"] == 3_575
    assert data["cycle_count"] == 2
    assert data["v1_estimated_input_tokens"] == 7_150
    assert data["v2_input_tokens"] == 1_000
    assert data["v2_input_tokens_per_cycle"] == 500
    assert data["tokens_saved"] == 6_150
    assert data["reduction_percentage"] == 86.01
    assert data["target_met"] is True
