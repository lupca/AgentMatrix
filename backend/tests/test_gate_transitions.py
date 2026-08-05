import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Agent,
    AgentRun,
    GateRecord,
    Project,
    ReviewCycle,
    Task,
    TaskEvent,
    TaskRound,
)
from app.services.task_orchestration import (
    ModeViolationError,
    PrerequisiteError,
    TaskOrchestrationService,
    TransitionConflictError,
    update_agent_success_rate,
)


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@executor",
            name="Executor",
            role="executor",
            cli="codex",
        )
    )
    db_session.add(
        Agent(
            id="@reviewer",
            name="Reviewer",
            role="reviewer",
            cli="codex",
        )
    )
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _add_terminal_review_run(db, task: Task, agent_id: str = "@reviewer") -> AgentRun:
    if task.current_round_id is None:
        task_round = TaskRound(
            id=f"{task.id}-round-1", task_id=task.id, round_no=1, status="in-review"
        )
        db.add(task_round)
        db.flush()
        task.current_round_id = task_round.id
    run = AgentRun(
        id=f"{task.id}-review-run",
        task_id=task.id,
        agent_id=agent_id,
        cli="codex",
        command="codex exec /code-review",
        kind="review",
        agent_role="reviewer",
        status="success",
        task_round_id=task.current_round_id,
    )
    db.add(run)
    db.flush()
    cycle = ReviewCycle(
        task_id=task.id,
        task_round_id=task.current_round_id,
        reviewer_id=agent_id,
        reviewer_agent_run_id=run.id,
        status="submitted",
    )
    db.add(cycle)
    db.commit()
    run.review_cycle_id = cycle.id
    return run


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Transition task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def test_plan_only_blocks_dispatch_and_records_rejection(service, db_session):
    task = _add_task(db_session, "MODE-001", mode="plan-only")

    with pytest.raises(ModeViolationError):
        service.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="plan-only-dispatch",
        )

    record = db_session.query(GateRecord).one()
    assert record.status == "rejected"
    assert record.mode == "plan-only"
    assert task.status == "todo"


def test_supervised_gate_emits_pending_event_once(service, db_session):
    task = _add_task(db_session, "GATE-INBOX", mode="supervised")

    for _ in range(2):
        service.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="gate-inbox-dispatch",
        )

    events = db_session.query(TaskEvent).filter_by(task_id=task.id).all()
    assert len(events) == 1
    assert events[0].event_type == "gate_pending"
    assert events[0].payload["gate"] == "dispatch"
    assert events[0].payload["gate_record_id"] > 0


def test_executor_success_stops_at_awaiting_review(service, db_session):
    task = _add_task(
        db_session,
        "EXEC-001",
        status="dispatched",
        executor="@executor",
    )

    result = service.record_execution_success(
        task_id=task.id,
        result_ref="abc123",
        actor="@executor",
        idempotency_key="run-1:success",
        run_id="run-1",
    )

    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.completed_at is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"status": "awaiting-review"}, "expected status"),
        ({"reviewer": None}, "reviewer is required"),
        ({"result_ref": None}, "result_ref is required"),
    ],
)
def test_verdict_rejects_missing_prerequisites(
    service,
    db_session,
    changes,
    message,
):
    values = {
        "status": "in-review",
        "executor": "@executor",
        "reviewer": "@reviewer",
        "result_ref": "abc123",
    }
    values.update(changes)
    task = _add_task(db_session, f"VER-{len(db_session.new)}{message[:2]}", **values)
    review_cycle_id = None
    if task.reviewer:
        run = _add_terminal_review_run(db_session, task, agent_id=task.reviewer)
        review_cycle_id = run.review_cycle_id

    with pytest.raises((PrerequisiteError, TransitionConflictError), match=message):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor=task.reviewer or "@reviewer",
            idempotency_key=f"verdict:{task.id}",
            review_cycle_id=review_cycle_id,
        )


def test_passing_verdict_requires_complete_passing_ac_results(service, db_session):
    task = _add_task(
        db_session,
        "VER-AC",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
        acceptance_criteria=["AC 1", "AC 2"],
    )
    run = _add_terminal_review_run(db_session, task)

    with pytest.raises(PrerequisiteError, match="incomplete"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key="verdict-incomplete",
            review_cycle_id=run.review_cycle_id,
        )
    with pytest.raises(PrerequisiteError, match="every acceptance criterion"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}, {"passed": False}],
            actor="@reviewer",
            idempotency_key="verdict-failed-ac",
            review_cycle_id=run.review_cycle_id,
        )


def test_only_passing_verdict_service_transition_reaches_done(
    service,
    db_session,
):
    task = _add_task(
        db_session,
        "VER-PASS",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
    )
    run = _add_terminal_review_run(db_session, task)

    result = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="verdict-pass",
        review_cycle_id=run.review_cycle_id,
    )

    assert result.task.status == "done"
    assert result.task.completed_at is not None
    assert result.gate_record.status == "approved"
    assert result.gate_record.output_ref == "pass"


def test_database_rejects_done_without_completion_fields(db_session):
    task = Task(
        id="INVALID-DONE",
        project="project",
        title="Invalid done task",
        status="done",
    )
    db_session.add(task)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_review_dispatch_at_awaiting_review_creates_review_run(service, db_session):
    task = _add_task(
        db_session,
        "REVDISP-001",
        status="awaiting-review",
        executor="@executor",
        result_ref="abc123",
    )

    result = service.request_dispatch(
        task_id=task.id,
        agent_id="@reviewer",
        actor="@operator",
        idempotency_key="review-dispatch-1",
        kind="review",
    )

    assert result.task.status == "dispatched"
    assert result.task.reviewer == "@reviewer"
    assert result.task.executor == "@executor"
    run = db_session.query(AgentRun).filter(AgentRun.task_id == task.id).one()
    assert run.kind == "review"
    assert run.agent_role == "reviewer"


def test_execute_dispatch_at_awaiting_review_still_conflicts(service, db_session):
    task = _add_task(
        db_session,
        "REVDISP-002",
        status="awaiting-review",
        executor="@executor",
        result_ref="abc123",
    )

    with pytest.raises(TransitionConflictError, match="expected status"):
        service.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="execute-dispatch-conflict",
        )


def test_review_dispatch_rejects_reviewer_equal_to_executor(service, db_session):
    task = _add_task(
        db_session,
        "REVDISP-003",
        status="awaiting-review",
        executor="@executor",
        result_ref="abc123",
    )

    with pytest.raises(PrerequisiteError, match="differ from executor"):
        service.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="review-dispatch-four-eyes",
            kind="review",
        )


def test_request_review_creates_review_run_and_moves_task_in_review(
    service, db_session
):
    task = _add_task(
        db_session,
        "REQREV-001",
        status="awaiting-review",
        executor="@executor",
        result_ref="base-sha..head-sha",
    )

    result = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key="request-review-1",
    )

    assert result.task.status == "in-review"
    assert result.task.reviewer == "@reviewer"
    run = db_session.query(AgentRun).filter(AgentRun.task_id == task.id).one()
    assert run.kind == "review"
    assert run.agent_role == "reviewer"
    assert run.status == "queued"
    assert "/code-review --from base-sha --to head-sha" in run.command


def test_request_review_rejects_reviewer_equal_to_executor(service, db_session):
    task = _add_task(
        db_session,
        "REQREV-002",
        status="awaiting-review",
        executor="@executor",
        result_ref="base-sha..head-sha",
    )

    with pytest.raises(PrerequisiteError, match="differ from executor"):
        service.request_review(
            task_id=task.id,
            reviewer="@executor",
            actor="@operator",
            idempotency_key="request-review-four-eyes",
        )


def test_request_review_requires_base_head_range_not_inferred(service, db_session):
    task = _add_task(
        db_session,
        "REQREV-003",
        status="awaiting-review",
        executor="@executor",
        result_ref="single-ref-no-range",
    )

    with pytest.raises(PrerequisiteError, match="base..head range"):
        service.request_review(
            task_id=task.id,
            reviewer="@reviewer",
            actor="@operator",
            idempotency_key="request-review-no-range",
        )


def test_verdict_rejects_when_no_review_run_exists(service, db_session):
    task = _add_task(
        db_session,
        "VERDICT-NORUN",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base-sha..head-sha",
    )

    with pytest.raises(PrerequisiteError, match="review_cycle_id is required"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key="verdict-no-review-run",
            review_cycle_id=None,
        )


def test_verdict_actor_cannot_impersonate_reviewer_without_a_review_run(
    service, db_session
):
    """CTV2-087: a caller cannot self-sign a verdict merely by claiming to be
    task.reviewer — a terminal AgentRun(kind="review") must actually exist."""
    task = _add_task(
        db_session,
        "VERDICT-SPOOF",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base-sha..head-sha",
    )

    with pytest.raises(PrerequisiteError, match="review_cycle_id is required"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key="verdict-spoofed-actor",
            review_cycle_id=None,
        )


def test_verdict_pass_with_three_ac_and_two_results_is_rejected(service, db_session):
    """CTV2-091 Verification: 3 AC but only 2 ac_results must be rejected —
    the old `max(1, len(acceptance_criteria))` let an empty-AC task pass
    verdict with a single fabricated result; this is the exact fake-done
    shape the gate must close."""
    task = _add_task(
        db_session,
        "VER-091-INCOMPLETE",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
        acceptance_criteria=["AC 1", "AC 2", "AC 3"],
    )
    run = _add_terminal_review_run(db_session, task)

    with pytest.raises(PrerequisiteError, match="incomplete"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}, {"passed": True}],
            actor="@reviewer",
            idempotency_key="verdict-3ac-2results",
            review_cycle_id=run.review_cycle_id,
        )


def test_update_agent_success_rate_ema_calculation(service, db_session):
    agent = Agent(
        id="ema_agent",
        name="EMA Agent",
        role="executor",
        success_rate=0.0,
    )
    db_session.add(agent)
    db_session.commit()

    # Pass (outcome 1.0): 0.1 * 1.0 + 0.9 * 0.0 = 0.1
    rate = update_agent_success_rate(db_session, "ema_agent", 1.0)
    assert rate == pytest.approx(0.1)
    assert agent.success_rate == pytest.approx(0.1)

    # Pass again (outcome 1.0): 0.1 * 1.0 + 0.9 * 0.1 = 0.19
    rate = update_agent_success_rate("ema_agent", 1.0, db=db_session)
    assert rate == pytest.approx(0.19)
    assert agent.success_rate == pytest.approx(0.19)

    # Fail (outcome 0.0): 0.1 * 0.0 + 0.9 * 0.19 = 0.171
    rate = update_agent_success_rate(db_session, "ema_agent", 0.0)
    assert rate == pytest.approx(0.171)
    assert agent.success_rate == pytest.approx(0.171)


def test_verdict_pass_auto_updates_executor_and_reviewer_success_rate(service, db_session):
    executor = db_session.query(Agent).filter(Agent.id == "@executor").first()
    reviewer = db_session.query(Agent).filter(Agent.id == "@reviewer").first()
    executor.success_rate = 0.5
    reviewer.success_rate = 0.5
    db_session.commit()

    task = _add_task(
        db_session,
        "VER-AUTO-PASS",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
    )
    run = _add_terminal_review_run(db_session, task)

    result = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="verdict-auto-pass",
        review_cycle_id=run.review_cycle_id,
    )

    assert result.task.status == "done"
    # new_rate = 0.1 * 1.0 + 0.9 * 0.5 = 0.55
    assert executor.success_rate == pytest.approx(0.55)
    assert reviewer.success_rate == pytest.approx(0.55)


def test_verdict_changes_auto_updates_executor_and_reviewer_success_rate(service, db_session):
    executor = db_session.query(Agent).filter(Agent.id == "@executor").first()
    reviewer = db_session.query(Agent).filter(Agent.id == "@reviewer").first()
    executor.success_rate = 0.5
    reviewer.success_rate = 0.5
    db_session.commit()

    task = _add_task(
        db_session,
        "VER-AUTO-FAIL",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="abc123",
    )
    run = _add_terminal_review_run(db_session, task)

    result = service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key="verdict-auto-fail",
        review_cycle_id=run.review_cycle_id,
    )

    assert result.task.status == "changes-requested"
    # new_rate = 0.1 * 0.0 + 0.9 * 0.5 = 0.45
    assert executor.success_rate == pytest.approx(0.45)
    assert reviewer.success_rate == pytest.approx(0.45)


def test_rejected_verdict_gate_returns_to_reviewable_state(service, db_session):
    task = _add_task(
        db_session,
        "VERDICT-GATE-REJECTED",
        mode="supervised",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base-sha..head-sha",
    )
    run = _add_terminal_review_run(db_session, task)
    pending = service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key="verdict-to-reject",
        review_cycle_id=run.review_cycle_id,
    )

    result = service.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="rejected",
        actor="@supervisor",
        idempotency_key="reject-verdict-gate",
    )

    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.awaiting_approval is False
    assert result.task.verdict is None
    assert result.gate_record.parent_id == pending.gate_record.id

    from app.mcp_native import _next_step

    assert _next_step({"task": {"status": result.task.status}}) == (
        "Gọi request_review để bắt đầu review độc lập."
    )

    # The resulting state has a legal next transition instead of being stuck.
    next_review = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key="review-after-rejected-verdict",
    )
    assert next_review.gate_record.gate_type == "review_order"
    assert next_review.gate_record.status == "pending"


def test_landing_rejects_forged_task_pass_without_approved_verdict(service, db_session):
    task = _add_task(
        db_session,
        "FORGED-PASS",
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base-sha..head-sha",
        verdict="pass",
    )

    with pytest.raises(PrerequisiteError, match="no approved pass verdict"):
        service.land_task(task_id=task.id, actor="@executor")

    db_session.refresh(task)
    assert task.status == "in-review"


def test_update_agent_success_rate_handles_missing_agent(service, db_session):
    res = update_agent_success_rate(db_session, "non_existent_agent", 1.0)
    assert res is None
