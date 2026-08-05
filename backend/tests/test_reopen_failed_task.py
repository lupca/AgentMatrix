"""A ``failed`` task must not be a black hole.

Two incidents on 2026-08-05 put good work permanently out of reach:

* CTV2-1382 -- the token brake fired one second *after* the executor's
  ``execution`` gate was approved and ``result_ref`` was written, flipping the
  task to ``failed``.  A commit with 830 passing tests was stranded on its
  branch: ``request_review`` wanted ``awaiting-review``, ``attach_result``
  wanted ``dispatched``, ``land_task`` wanted an approved verdict, and a newly
  requested run was cancelled on arrival with "Task reached terminal state:
  failed".

* CTV2-1388 -- the task filed to fix the above was escalated to ``failed`` by
  the orchestration driver because its plan critic had not finished yet, and
  died the same way.  The defect killed its own fix.

These tests pin both halves of the repair: the brake stops spending without
destroying a delivered result, and there is a way back when a task does end up
``failed``.
"""

import pytest

from app.db.models import Agent, AgentRun, GateRecord, Project, Task
from app.services.task_orchestration import (
    BrakeViolationError,
    OrchestrationError,
    PrerequisiteError,
    TaskOrchestrationService,
)
from app.services.task_validators import BrakeDecision, TaskValidator


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Reopen task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def _fire_token_brake(db, task: Task) -> None:
    TaskValidator(db)._record_brake(
        task,
        BrakeDecision(
            False,
            "Task token limit reached: 56,011,067 >= 40,000,000 tokens",
            "token_limit",
        ),
    )


def test_budget_brake_leaves_a_delivered_result_reviewable(service, db_session):
    """The exact CTV2-1382 shape: result already delivered, then the brake."""

    task = _add_task(
        db_session,
        "BRAKE-001",
        status="awaiting-review",
        executor="@executor",
        result_ref="52e96501bc73..a6c25818e117",
        current_gate="review_order",
    )

    _fire_token_brake(db_session, task)
    db_session.refresh(task)

    assert task.status == "awaiting-review"
    # The brake still has to be visible and still has to stop further spend --
    # it just may not throw the finished work away.
    assert task.awaiting_approval is True
    assert "token limit" in (task.error or "")
    assert db_session.query(GateRecord).filter_by(
        task_id=task.id, gate_type="safety_brake"
    ).count() == 1

    decision = service.check_brakes(task, for_spawn=True, audit=False)
    assert decision.allowed is False
    assert decision.code == "pending_gate"
    with pytest.raises(BrakeViolationError, match="pending gate"):
        service.request_review(
            task_id=task.id,
            reviewer="@reviewer",
            actor="chat:test",
            idempotency_key="review-blocked-by-brake",
        )
    assert db_session.query(AgentRun).filter_by(task_id=task.id).count() == 0


def test_budget_brake_still_fails_a_task_with_nothing_delivered(service, db_session):
    """No result yet means nothing to protect: the old behaviour must stand."""

    task = _add_task(
        db_session,
        "BRAKE-002",
        status="dispatched",
        executor="@executor",
    )

    _fire_token_brake(db_session, task)
    db_session.refresh(task)

    assert task.status == "failed"
    assert db_session.query(AgentRun).filter_by(task_id=task.id).count() == 0


@pytest.mark.parametrize(
    ("code", "task_id"),
    [
        ("autonomy_disabled", "BRAKE-AUTO"),
        ("cost_limit", "BRAKE-COST"),
        ("token_limit", "BRAKE-TOKEN"),
    ],
)
def test_each_budget_brake_blocks_new_review_spend_after_delivery(
    service, db_session, code, task_id
):
    task = _add_task(
        db_session,
        task_id,
        status="awaiting-review",
        executor="@executor",
        result_ref="base-sha..head-sha",
        current_gate="review_order",
    )
    TaskValidator(db_session)._record_brake(
        task, BrakeDecision(False, f"{code} fired", code)
    )

    db_session.refresh(task)
    assert task.status == "awaiting-review"
    assert task.awaiting_approval is True
    assert service.check_brakes(task, for_spawn=True).code == "pending_gate"
    with pytest.raises(BrakeViolationError):
        service.request_review(
            task_id=task.id,
            reviewer="@reviewer",
            actor="chat:test",
            idempotency_key=f"{task_id}:review-blocked",
        )
    assert db_session.query(AgentRun).filter_by(task_id=task.id).count() == 0


def test_reopen_sends_a_delivered_task_back_to_the_review_boundary(service, db_session):
    brake = GateRecord(
        task_id="REOPEN-001",
        gate_type="safety_brake",
        status="rejected",
        actor="system:safety-brake",
        mode="bypass",
        input_payload={"code": "token_limit"},
        error_message="Task token limit reached",
    )
    db_session.add(brake)
    task = _add_task(
        db_session,
        "REOPEN-001",
        status="failed",
        executor="@executor",
        result_ref="abc123",
        error="Task token limit reached",
        awaiting_approval=True,
    )

    result = service.reopen_failed_task(task_id=task.id, actor="chat:test")

    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.error is None
    assert result.task.awaiting_approval is False
    # Append-only: reopening adds a record, it does not edit the escalation
    # that closed the task.
    assert result.gate_record.gate_type == "reopen"
    assert result.gate_record.status == "approved"
    assert result.gate_record.parent_id == brake.id
    assert db_session.get(GateRecord, brake.id).status == "rejected"


def test_reopen_sends_an_undelivered_task_back_to_todo(service, db_session):
    task = _add_task(
        db_session,
        "REOPEN-002",
        status="failed",
        error="generated plan has no current independent critic acceptance",
        awaiting_approval=True,
    )

    result = service.reopen_failed_task(task_id=task.id, actor="chat:test")

    assert result.task.status == "todo"
    assert result.task.current_gate == "dispatch"


@pytest.mark.asyncio
async def test_reopen_handler_returns_a_serializable_payload(service, db_session):
    """Cover the handler, not just the service.

    The first cut of these tests exercised ``reopen_failed_task`` directly and
    passed, while the MCP handler still read ``result.record`` (the field is
    ``gate_record``).  Every real call blew up with an internal_error *after*
    the transition had already committed -- the task moved, the caller was
    told it had not.  A green service-level test proved nothing about the path
    users actually take.
    """

    from app.services.command_router import CommandRouter

    _add_task(
        db_session,
        "REOPEN-005",
        status="failed",
        executor="@executor",
        result_ref="abc123",
    )

    response = await CommandRouter(db_session)._handle_reopen_task(
        "REOPEN-005", "test-session"
    )

    assert "error" not in response
    assert response["action"] == "reopened"
    assert response["status"] == "awaiting-review"
    assert isinstance(response["gate_record_id"], int)


def test_missing_plan_critic_acceptance_waits_instead_of_killing_the_task(
    service, db_session
):
    """A critic that has not answered yet is a wait, not a death sentence.

    ``_advance_todo`` used to escalate here, which sets ``failed`` -- terminal,
    so ``_sync_after_transition`` cancelled the critic run that was about to
    answer and rejected every pending gate.  On 2026-08-05 this fired on
    CTV2-1388, the task filed to fix this very class of bug, seconds after its
    plan was generated.
    """

    from app.workers.agent_runner import _advance_todo

    task = _add_task(
        db_session,
        "CRITIC-001",
        status="todo",
        planner="@executor",
        plan_critic_status=None,
    )

    outcome = _advance_todo(db_session, service, task)
    db_session.refresh(task)

    assert outcome == "waiting_plan_critic"
    assert task.status == "todo"
    # The guard still holds: no plan critic acceptance means no dispatch.
    assert task.executor is None


def test_reopen_refuses_a_task_that_is_not_failed(service, db_session):
    task = _add_task(db_session, "REOPEN-003", status="dispatched", executor="@executor")

    with pytest.raises(PrerequisiteError, match="requires status 'failed'"):
        service.reopen_failed_task(task_id=task.id, actor="chat:test")


def test_reopen_does_not_hand_out_a_verdict(service, db_session):
    """Reopening restores the review boundary; it must never skip four-eyes."""

    task = _add_task(
        db_session,
        "REOPEN-004",
        status="failed",
        executor="@executor",
        result_ref="abc123",
    )

    service.reopen_failed_task(task_id=task.id, actor="chat:test")
    db_session.refresh(task)

    # Still needs an independent reviewer and an approved pass verdict before
    # anything can land -- reopen only undid the terminal status.
    assert task.status == "awaiting-review"
    assert task.landed_ref is None
    with pytest.raises(OrchestrationError):
        service.land_task(task_id=task.id, actor="chat:test")


def test_failed_delivered_result_recovers_through_independent_review_and_land(
    service, db_session
):
    """The MCP lifecycle remains four-eyes after a terminal failure."""

    brake = GateRecord(
        task_id="REOPEN-FLOW",
        gate_type="safety_brake",
        status="rejected",
        actor="system:safety-brake",
        mode="supervised",
        input_payload={"code": "token_limit"},
        error_message="Task token limit reached",
    )
    db_session.add(brake)
    task = _add_task(
        db_session,
        "REOPEN-FLOW",
        mode="supervised",
        status="failed",
        executor="@executor",
        result_ref="base-sha..head-sha",
    )

    reopened = service.reopen_failed_task(task_id=task.id, actor="chat:test")
    assert reopened.task.status == "awaiting-review"
    assert reopened.gate_record.parent_id == brake.id

    review_request = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="chat:test",
        idempotency_key="reopen-flow-review",
    )
    assert review_request.applied is False
    review_gate = review_request.gate_record
    assert review_gate.gate_type == "review_order"

    reviewed = service.decide_gate(
        gate_record_id=review_gate.id,
        decision="approved",
        actor="chat:supervisor",
        idempotency_key="reopen-flow-review-approve",
    )
    assert reviewed.task.status == "in-review"
    assert reviewed.agent_run is not None
    assert reviewed.agent_run.agent_id == "@reviewer"
    assert reviewed.agent_run.agent_id != reviewed.task.executor
    reviewed.agent_run.status = "success"
    db_session.commit()

    verdict_request = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="reopen-flow-verdict",
    )
    assert verdict_request.applied is False
    verdict_gate = verdict_request.gate_record

    verdict = service.decide_gate(
        gate_record_id=verdict_gate.id,
        decision="approved",
        actor="chat:supervisor",
        idempotency_key="reopen-flow-verdict-approve",
    )
    assert verdict.task.status == "done"
    assert verdict.task.verdict == "pass"
    assert verdict.task.landed_ref is None  # /tmp is not a project repository

    landed = service.land_task(task_id=task.id, actor="chat:supervisor")
    assert landed["status"] == "done"
    assert db_session.get(GateRecord, brake.id).status == "rejected"


def test_escalation_can_be_cleared_by_approving_it(service, db_session):
    """An escalation must be resolvable, or it is just a nicer dead end.

    The first cut of this fix stopped escalation from marking the task
    `failed` but left the gate record written as `rejected`.  That was worse
    than it looked: `awaiting_approval` blocked every dispatch, while
    `approve_gate` answered "No pending gate found" -- nothing could clear it.
    CTV2-1389 sat there with a finished, critic-accepted plan it could not act
    on.

    Written as a pending gate, the block and its release come from the same
    place.
    """

    task = _add_task(db_session, "ESC-CLEAR", status="todo")

    record = service.escalate_task(
        task_id=task.id,
        reason="advance_task made no progress after 3 calls at status 'todo'",
        actor="system:worker",
    )
    db_session.refresh(task)

    assert record.status == "pending"
    assert task.status == "todo"
    assert task.awaiting_approval is True

    result = service.decide_gate(
        gate_record_id=record.id,
        decision="approved",
        actor="chat:test",
        idempotency_key="esc-clear:approve",
    )
    db_session.refresh(task)

    assert result.gate_record.parent_id == record.id
    assert task.awaiting_approval is False
    assert task.approval_prompt is None
    assert task.error is None
    assert task.status == "todo"
    # Append-only: the escalation row itself is untouched.
    assert db_session.get(GateRecord, record.id).status == "pending"


def test_reopen_clears_a_stale_approval_projection(service, db_session):
    """A projection that drifted from the ledger needs one way back.

    `awaiting_approval` blocks every dispatch path, and `approve_gate` refuses
    a task with no unresolved pending gate ("No pending gate found").  When
    the flag is set but the ledger holds nothing pending -- as CTV2-1389 was
    left after escalations changed shape -- the task is stuck with no tool
    able to touch it.
    """

    task = _add_task(
        db_session,
        "STALE-001",
        status="todo",
        awaiting_approval=True,
        approval_prompt="left over from an older escalation shape",
        error="left over from an older escalation shape",
    )

    result = service.reopen_failed_task(task_id=task.id, actor="chat:test")
    db_session.refresh(task)

    assert task.status == "todo"
    assert task.awaiting_approval is False
    assert task.approval_prompt is None
    assert task.error is None
    assert result.gate_record.gate_type == "reopen"


def test_reopen_still_refuses_a_healthy_task(service, db_session):
    """Only stuck tasks: a task with nothing wrong must not be touched."""

    task = _add_task(db_session, "STALE-002", status="dispatched", executor="@executor")

    with pytest.raises(PrerequisiteError, match="requires status 'failed'"):
        service.reopen_failed_task(task_id=task.id, actor="chat:test")


def test_gate_blocked_rounds_are_not_evidence_of_a_stall(db_session):
    """Approving an escalation must not immediately re-trip the stall guard.

    Rounds where the driver was blocked by a pending gate did not try, so they
    cannot show the task is stuck.  Counting them made a loop: escalate -> a
    human approves -> the driver runs, sees the very rounds its own escalation
    caused, and escalates again.  CTV2-1389 went round it twice on 2026-08-05
    while holding a finished, critic-accepted plan.
    """

    from app.db.models import AuditLog
    from app.workers.agent_runner import _advance_task_stalled

    def _round(outcome: str) -> None:
        db_session.add(
            AuditLog(
                task_id="STALL-001",
                action="advance_task:manual",
                actor="system:orchestration-driver",
                details={
                    "status_before": "todo",
                    "status_after": "todo",
                    "outcome": outcome,
                },
            )
        )

    for outcome in ("gate_pending", "escalated_stall", "gate_pending"):
        _round(outcome)
    db_session.commit()
    assert _advance_task_stalled(db_session, "STALL-001", "todo") is False

    # Rounds that genuinely tried and got nowhere still trip it.
    for _ in range(3):
        _round("brake:autonomy_disabled")
    db_session.commit()
    assert _advance_task_stalled(db_session, "STALL-001", "todo") is True


def test_review_request_is_idempotent_when_only_the_score_text_moves(
    service, db_session
):
    """Telemetry inside the reason must not change the request's identity.

    `selection_reason` embeds "score=0.89, success_rate=100%" -- numbers that
    move whenever any agent finishes a task.  Hashing them made the same
    logical request (same task, round and reviewer) collide with its own
    stored idempotency key, and CTV2-1389 looped: escalate, clear, escalate
    again on the next driver pass, while holding a finished commit.
    """

    task = _add_task(
        db_session,
        "IDEM-001",
        status="awaiting-review",
        executor="@executor",
        result_ref="base..head",
        current_gate="review_order",
    )

    first = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="system:orchestration-driver",
        idempotency_key="advance:IDEM-001:review:r0:reviewer:@reviewer",
        selection_reason="@reviewer selected by matcher: score=0.89, success_rate=100%",
    )

    # Same request, but every agent's measured rate has moved since.
    second = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="system:orchestration-driver",
        idempotency_key="advance:IDEM-001:review:r0:reviewer:@reviewer",
        selection_reason="@reviewer selected by matcher: score=0.71, success_rate=83%",
    )

    assert second.gate_record.id == first.gate_record.id



def test_review_retry_after_a_resolved_attempt_gets_a_fresh_key(service, db_session):
    """A retry must not reuse a spent idempotency key.

    `(task_id, idempotency_key)` is UNIQUE, so a key is spent the moment its
    request is stored and no hash behind it can ever be rewritten.  CTV2-1389
    looped on exactly that: the driver reused one key per (task, round,
    reviewer), the stored hash stopped matching, and every pass raised the
    conflict, escalated, was cleared, and escalated again -- while the task held
    a finished commit waiting to be reviewed.
    """

    task = _add_task(
        db_session,
        "IDEM-002",
        status="awaiting-review",
        executor="@executor",
        result_ref="base..head",
        current_gate="review_order",
        mode="supervised",
    )

    assert service.review_gate_count(task.id, round_=0) == 0

    service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="system:orchestration-driver",
        idempotency_key="advance:IDEM-002:review:r0:a0:reviewer:@reviewer",
        selection_reason="matcher pick",
    )

    # The counter moved, so the next attempt cannot collide with the first.
    assert service.review_gate_count(task.id, round_=0) == 1
