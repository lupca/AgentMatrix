"""review_cycles / review_findings — queryable verdict/finding home (CTV2-1379).

Covers: lifecycle transitions, a retry creating a NEW cycle row (not mutating
the old one), abandonment on a dead review run, finding CRUD with mandatory
waive reason, backfill idempotency, and the cycle-bound verdict check that
closes the four-eyes-by-TIME hole (a stale round's successful review run can
no longer authorize a verdict for the current round).
"""

import pytest

from app.db.models import (
    Agent,
    AgentRun,
    GateRecord,
    Project,
    ReviewCycle,
    ReviewFinding,
    Task,
    TaskRound,
)
from app.services.task_orchestration import PrerequisiteError, TaskOrchestrationService


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.add(Agent(id="@other-reviewer", name="Other Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _dispatch_to_review(db, service, task_id, reviewer="@reviewer"):
    task = Task(
        id=task_id,
        project="project",
        title="Review cycle task",
        mode="bypass",
        acceptance_criteria=["AC 1"],
    )
    db.add(task)
    db.commit()
    service.request_dispatch(
        task_id=task_id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=f"{task_id}:dispatch",
    )
    execute_run = db.query(AgentRun).filter(AgentRun.task_id == task_id).one()
    execute_run.status = "success"
    db.commit()
    service.record_execution_success(
        task_id=task_id,
        result_ref="base..head",
        actor="agent:@executor",
        idempotency_key=f"{task_id}:exec-success",
        run_id=execute_run.id,
    )
    result = service.request_review(
        task_id=task_id,
        reviewer=reviewer,
        actor="@operator",
        idempotency_key=f"{task_id}:review",
    )
    return db.get(Task, task_id), result.agent_run


# --- lifecycle ---------------------------------------------------------


def test_review_order_creates_requested_cycle(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-001")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    assert cycle.status == "requested"
    assert cycle.reviewer_agent_run_id == run.id
    assert cycle.reviewer_id == "@reviewer"
    assert cycle.task_round_id == task.current_round_id


def test_verdict_submission_moves_cycle_to_submitted_then_final(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-002")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()

    result = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict",
        review_cycle_id=cycle.id,
    )

    db_session.refresh(cycle)
    assert result.task.status == "done"
    assert cycle.status == "pass"
    assert cycle.verdict == "pass"
    assert cycle.submitted_at is not None
    assert cycle.completed_at is not None


def test_changes_verdict_sets_cycle_status_changes(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-003")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()

    result = service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict",
        review_cycle_id=cycle.id,
    )

    db_session.refresh(cycle)
    assert result.task.status == "changes-requested"
    assert cycle.status == "changes"
    assert cycle.verdict == "changes"


def test_review_run_death_abandons_the_cycle(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-004")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()

    service.record_review_failure(
        task_id=task.id,
        error="reviewer crashed",
        actor="system:watchdog",
        idempotency_key=f"{task.id}:review-fail",
        run_id=run.id,
    )

    db_session.refresh(cycle)
    assert cycle.status == "abandoned"


def test_review_run_death_after_verdict_does_not_reopen_a_final_cycle(db_session, service):
    """A cycle that already reached pass/changes/submitted must never be
    clobbered back to 'abandoned' by a late-arriving failure signal."""
    task, run = _dispatch_to_review(db_session, service, "RC-004B")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()
    service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict",
        review_cycle_id=cycle.id,
    )
    db_session.refresh(cycle)
    assert cycle.status == "changes"

    from app.services.task_state_machine import TaskStateMachine

    TaskStateMachine(db_session)._abandon_review_cycle(run.id)
    db_session.refresh(cycle)
    assert cycle.status == "changes"


# --- retry creates a NEW row, does not mutate the old one ---------------


def test_retry_creates_a_new_cycle_row_for_the_new_round(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-005")
    first_cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()
    service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict-1",
        review_cycle_id=first_cycle.id,
    )
    db_session.refresh(first_cycle)
    assert first_cycle.status == "changes"

    # Redispatch: new round -> new review -> new cycle row.
    service.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=f"{task.id}:dispatch-2",
    )
    new_run = (
        db_session.query(AgentRun)
        .filter(AgentRun.task_id == task.id, AgentRun.kind == "execute")
        .order_by(AgentRun.queued_at.desc())
        .first()
    )
    new_run.status = "success"
    db_session.commit()
    service.record_execution_success(
        task_id=task.id,
        result_ref="base2..head2",
        actor="agent:@executor",
        idempotency_key=f"{task.id}:exec-success-2",
        run_id=new_run.id,
    )
    service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key=f"{task.id}:review-2",
    )

    cycles = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).all()
    assert len(cycles) == 2
    # The old row is untouched.
    db_session.refresh(first_cycle)
    assert first_cycle.status == "changes"
    second_cycle = [c for c in cycles if c.id != first_cycle.id][0]
    assert second_cycle.status == "requested"
    assert second_cycle.task_round_id != first_cycle.task_round_id


# --- verdict must be bound to the CURRENT cycle/round --------------------


def test_verdict_from_a_stale_round_cycle_is_rejected(db_session, service):
    """The exact hole this table closes: a successful review run from an
    EARLIER round must not authorize a verdict on the CURRENT round, even
    though the reviewer identity matches task.reviewer."""
    task, run = _dispatch_to_review(db_session, service, "RC-006")
    stale_cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()
    service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict-1",
        review_cycle_id=stale_cycle.id,
    )

    service.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=f"{task.id}:dispatch-2",
    )
    new_run = (
        db_session.query(AgentRun)
        .filter(AgentRun.task_id == task.id, AgentRun.kind == "execute")
        .order_by(AgentRun.queued_at.desc())
        .first()
    )
    new_run.status = "success"
    db_session.commit()
    service.record_execution_success(
        task_id=task.id,
        result_ref="base2..head2",
        actor="agent:@executor",
        idempotency_key=f"{task.id}:exec-success-2",
        run_id=new_run.id,
    )
    service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key=f"{task.id}:review-2",
    )

    # Attempt a verdict against the STALE (round 1) cycle while the task is
    # now on round 2 -- must be refused, no fallback.
    with pytest.raises(PrerequisiteError, match="current round"):
        service.request_verdict(
            task_id=task.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key=f"{task.id}:verdict-stale",
            review_cycle_id=stale_cycle.id,
        )


def test_verdict_for_a_different_task_cycle_is_rejected(db_session, service):
    task_a, run_a = _dispatch_to_review(db_session, service, "RC-007A")
    task_b, run_b = _dispatch_to_review(db_session, service, "RC-007B")
    cycle_b = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task_b.id).one()
    run_a.status = "success"
    db_session.commit()

    with pytest.raises(PrerequisiteError, match="does not exist for task"):
        service.request_verdict(
            task_id=task_a.id,
            verdict="pass",
            ac_results=[{"passed": True}],
            actor="@reviewer",
            idempotency_key=f"{task_a.id}:verdict-cross",
            review_cycle_id=cycle_b.id,
        )


# --- findings ------------------------------------------------------------


def test_findings_are_stored_as_rows_on_verdict(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-008")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    run.status = "success"
    db_session.commit()

    service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key=f"{task.id}:verdict",
        review_cycle_id=cycle.id,
        findings=[
            {
                "id": "f1",
                "severity": "high",
                "category": "bug",
                "file": "app/foo.py",
                "line": 10,
                "description": "off-by-one",
            }
        ],
    )

    findings = db_session.query(ReviewFinding).filter(ReviewFinding.review_cycle_id == cycle.id).all()
    assert len(findings) == 1
    assert findings[0].status == "open"
    assert findings[0].severity == "high"
    assert "off-by-one" in findings[0].title


def test_finding_crud_and_waive_requires_reason(db_session, service):
    task, run = _dispatch_to_review(db_session, service, "RC-009")
    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    finding = ReviewFinding(
        review_cycle_id=cycle.id,
        severity="minor",
        title="nit",
        status="open",
    )
    db_session.add(finding)
    db_session.commit()

    finding.status = "fixed"
    db_session.commit()
    assert finding.status == "fixed"

    finding.status = "waived"
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()

    finding = db_session.get(ReviewFinding, finding.id)
    finding.status = "waived"
    finding.waived_reason = "false positive, verified by hand"
    db_session.commit()
    assert finding.status == "waived"


# --- backfill idempotency -------------------------------------------------


def test_backfill_inserts_matching_rows_once(db_session):
    """Simulate the migration's backfill against the same schema the test
    suite already builds (sqlite via Base.metadata.create_all), proving the
    filter and the ON CONFLICT idempotency without touching real Postgres."""
    import importlib.util
    import os

    task = Task(id="BF-001", project="project-bf", title="Backfilled task", status="changes-requested")
    db_session.add(Project(id="project-bf", name="P", repo_root="/tmp"))
    db_session.add(task)
    db_session.commit()
    task_round = TaskRound(id="bf-round-1", task_id=task.id, round_no=1, status="done")
    db_session.add(task_round)
    db_session.commit()

    gate = GateRecord(
        task_id=task.id,
        gate_type="verdict",
        status="approved",
        actor="@reviewer",
        mode="bypass",
        idempotency_key="bf-verdict-1",
        input_hash="x",
        output_ref="pass",
        input_payload={"verdict": "pass", "ac_results": [{"passed": True}], "reviewer": "@reviewer"},
    )
    db_session.add(gate)
    db_session.commit()

    # A verdict gate with no ac_results must be skipped by the filter.
    empty_gate = GateRecord(
        task_id=task.id,
        gate_type="verdict",
        status="rejected",
        actor="@reviewer",
        mode="bypass",
        idempotency_key="bf-verdict-empty",
        input_hash="y",
        input_payload={"note": "no ac_results here"},
    )
    db_session.add(empty_gate)
    db_session.commit()

    module_path = os.path.join(
        os.path.dirname(__file__), "..", "alembic", "versions", "055_review_cycles.py"
    )
    spec = importlib.util.spec_from_file_location("review_cycles_migration", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module._backfill(db_session.connection())
    db_session.commit()
    assert db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).count() == 1

    # Running it again must insert zero additional rows.
    module._backfill(db_session.connection())
    db_session.commit()
    assert db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).count() == 1

    cycle = db_session.query(ReviewCycle).filter(ReviewCycle.task_id == task.id).one()
    assert cycle.source_gate_record_id == gate.id
    assert cycle.verdict == "pass"
    assert cycle.status == "pass"
