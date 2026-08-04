import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from app.db.models import (
    Agent,
    AgentAccount,
    AgentRun,
    AuditLog,
    DispatchCandidate,
    DispatchDecision,
    GateRecord,
    LLMUsage,
    OutboxEvent,
    Project,
    Setting,
    SpecAnchor,
    SpecItem,
    SpecTaskLink,
    Task,
    TaskDependency,
    TaskRound,
)
from app.services.task_orchestration import (
    BrakeViolationError,
    DependencyCycleError,
    IdempotencyConflictError,
    PrerequisiteError,
    StaleIdempotencyRecordError,
    TaskNotFoundError,
    TaskOrchestrationService,
    TransitionConflictError,
)


@pytest.fixture
def orchestration(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(
        Agent(
            id="@executor",
            name="Executor",
            role="executor",
            cli="codex",
        )
    )
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _task(db, task_id: str, *, mode: str = "supervised") -> Task:
    task = Task(
        id=task_id,
        project="project",
        title="Governed task",
        mode=mode,
        acceptance_criteria=["Tests pass"],
    )
    db.add(task)
    db.commit()
    return task


def _dispatch_and_approve(orchestration, task, idempotency_key):
    """Dispatch under either mode and land on an approved run.

    Bypass tasks apply immediately. Supervised tasks land the dispatch as a
    *pending* record first, so this also drives the approval — the pending
    record (cached under `idempotency_key`) is exactly what a staleness-guard
    regression would see on replay, since the approved run lives on a child
    record instead (CTV2-088 round 3).
    """
    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=idempotency_key,
    )
    if task.mode == "supervised":
        result = orchestration.decide_gate(
            gate_record_id=result.gate_record.id,
            decision="approved",
            actor="@supervisor",
            idempotency_key=f"{idempotency_key}:approval",
        )
    return result


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_integration_branch_moves_only_after_independent_pass_verdict(
    orchestration, db_session, tmp_path
):
    """CTV2-225 reproducer for the governed execution-to-landing sequence."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "ct-run/ctv2-225-reproducer")
    (repo / "result.txt").write_text("review me\n")
    _git(repo, "add", "result.txt")
    _git(repo, "commit", "-q", "-m", "executor result")
    result_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    project = db_session.get(Project, "project")
    project.repo_root = str(repo)
    db_session.add(
        SpecItem(
            id="spec-result-file",
            project_id=project.id,
            kind="requirement",
            title="Reviewed results are landed",
            body="The governed result file is part of the delivered behavior.",
            status="active",
        )
    )
    db_session.add(
        SpecAnchor(
            id="anchor-result-file",
            spec_item_id="spec-result-file",
            repo=str(repo),
            path="result.txt",
            symbol="result",
            relation="implements",
            anchor_sha="a" * 64,
        )
    )
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    task = _task(db_session, "CTV2-225-REPRO", mode="bypass")
    task.status = "dispatched"
    task.executor = "@executor"
    db_session.commit()

    execution = orchestration.record_execution_success(
        task_id=task.id,
        result_ref=f"{base}..{result_head}",
        actor="agent:@executor",
        idempotency_key="ctv2-225:execution",
        run_id="executor-run",
    )
    assert execution.task.status == "awaiting-review"
    assert execution.gate_record.gate_type == "execution"
    assert _git(repo, "rev-parse", "main") == base
    assert db_session.query(OutboxEvent).count() == 0

    review = orchestration.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="system:orchestration-driver",
        idempotency_key="ctv2-225:review",
    )
    assert review.task.status == "in-review"
    assert review.agent_run is not None
    assert _git(repo, "rev-parse", "main") == base
    outbox = db_session.query(OutboxEvent).all()
    assert [event.event_type for event in outbox] == ["run_requested"]

    review.agent_run.status = "success"
    db_session.commit()
    verdict = orchestration.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True, "evidence": "review run completed"}],
        actor="@reviewer",
        idempotency_key="ctv2-225:verdict",
    )
    assert verdict.task.status == "done"
    assert verdict.task.landed_ref
    assert _git(repo, "merge-base", "--is-ancestor", result_head, "main") == ""
    landed_head = _git(repo, "rev-parse", "main")
    assert landed_head != base
    pass_records = (
        db_session.query(GateRecord)
        .filter_by(task_id=task.id, gate_type="verdict", status="approved")
        .all()
    )
    assert len(pass_records) == 1
    assert pass_records[0].output_ref == "pass"
    derived_link = db_session.query(SpecTaskLink).filter_by(
        task_id=task.id, spec_item_id="spec-result-file", relation="modifies"
    ).one()
    assert derived_link.confidence == "derived"
    assert derived_link.created_by == "system:landing"

    # Verdict approval already performs the merge. land_task is the explicit,
    # idempotent retry/backfill surface and must not create another commit.
    landed = orchestration.land_task(task_id=task.id, actor="@operator")
    assert landed["status"] == "done"
    assert _git(repo, "rev-parse", "main") == landed_head
    assert db_session.query(SpecTaskLink).filter_by(
        task_id=task.id, spec_item_id="spec-result-file", relation="modifies"
    ).count() == 1
    assert [event.event_type for event in db_session.query(OutboxEvent).all()] == [
        "run_requested",
        "graph_rebuild_requested",
    ]


@pytest.mark.parametrize(
    ("status", "awaiting_approval", "expected"),
    [
        ("todo", False, "executing"),
        ("dispatched", True, "waiting_human"),
        ("failed", False, "blocked"),
        ("failed", True, "waiting_human"),
        ("awaiting-review", False, "reviewing"),
        ("in-review", False, "reviewing"),
        ("changes-requested", False, "reviewing"),
        ("done", False, "terminal"),
        ("cancelled", False, "terminal"),
    ],
)
def test_workflow_state_is_derived_from_task_projection(
    status, awaiting_approval, expected
):
    task = Task(
        id="STATE-001",
        project="project",
        title="Workflow state",
        status=status,
        awaiting_approval=awaiting_approval,
    )

    assert task.workflow_state == expected


def test_escalate_task_clears_approval_projection_without_pending_gate(
    orchestration, db_session
):
    task = _task(db_session, "ESCALATE-001", mode="bypass")
    task.status = "dispatched"
    db_session.commit()

    record = orchestration.escalate_task(
        task_id=task.id, reason="Review output was invalid", actor="system:worker"
    )

    assert task.status == "failed"
    assert task.workflow_state == "blocked"
    assert task.awaiting_approval is False
    assert task.error == "Review output was invalid"
    assert task.approval_prompt is None
    assert record.gate_type == "escalation"
    assert record.status == "rejected"
    assert record.actor == "system:worker"
    assert record.error_message == "Review output was invalid"
    assert db_session.get(type(record), record.id) is not None


def test_new_supervised_gate_rejects_stale_same_type_gate(orchestration, db_session):
    task = _task(db_session, "GATE-STALE-001")

    first = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="stale-dispatch-1",
    )
    second = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="stale-dispatch-2",
    )

    stale_decision = (
        db_session.query(GateRecord)
        .filter(GateRecord.parent_id == first.gate_record.id)
        .one()
    )
    assert stale_decision.status == "rejected"
    assert stale_decision.actor == "system:stale-cleanup"
    assert stale_decision.error_message == "Superseded by newer gate request"
    assert second.gate_record.status == "pending"
    assert task.awaiting_approval is True


def test_terminal_failure_rejects_all_pending_gates_and_clears_flag(
    orchestration, db_session
):
    task = _task(db_session, "GATE-STALE-002")
    task.status = "dispatched"
    task.executor = "@executor"
    task.awaiting_approval = True
    for index, gate_type in enumerate(("review_order", "verdict"), start=1):
        db_session.add(
            GateRecord(
                task_id=task.id,
                gate_type=gate_type,
                status="pending",
                actor="@operator",
                mode="supervised",
                idempotency_key=f"pending-terminal-{index}",
                input_hash=str(index),
                input_payload={"gate_type": gate_type},
            )
        )
    db_session.commit()

    orchestration.record_execution_failure(
        task_id=task.id,
        error="executor failed",
        actor="@executor",
        idempotency_key="execution-failure-terminal-cleanup",
    )

    decisions = (
        db_session.query(GateRecord)
        .filter(
            GateRecord.task_id == task.id,
            GateRecord.parent_id.isnot(None),
        )
        .all()
    )
    assert task.status == "failed"
    assert task.awaiting_approval is False
    assert task.approval_prompt is None
    assert len(decisions) == 2
    assert {record.status for record in decisions} == {"rejected"}
    assert {record.actor for record in decisions} == {"system:terminal-cleanup"}


def test_terminal_task_cancels_active_agent_run(orchestration, db_session):
    task = _task(db_session, "GATE-TERMINAL-CANCEL")
    task.status = "dispatched"
    task.executor = "@executor"
    run = AgentRun(
        id="terminal-cancel-run",
        task_id=task.id,
        agent_id="@executor",
        cli="codex",
        command="sleep 60",
        status="running",
        pid=4242,
    )
    db_session.add(run)
    db_session.commit()

    orchestration.record_execution_failure(
        task_id=task.id,
        error="executor failed",
        actor="@executor",
        idempotency_key="terminal-cancel-failure",
        run_id=run.id,
        error_code="process-failed",
    )

    db_session.refresh(run)
    assert task.status == "failed"
    assert run.status == "cancelled"
    assert run.error_message == "Task reached terminal state: failed"


def test_terminal_done_rejects_pending_gate(orchestration, db_session):
    task = _task(db_session, "GATE-STALE-DONE", mode="bypass")
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    task.status = "in-review"
    task.executor = "@executor"
    task.reviewer = "@reviewer"
    task.result_ref = "base..head"
    task.awaiting_approval = True
    db_session.add_all([
        AgentRun(
            id="stale-done-review-run",
            task_id=task.id,
            agent_id="@reviewer",
            cli="codex",
            command="codex exec /code-review",
            kind="review",
            agent_role="reviewer",
            status="success",
        ),
        GateRecord(
            task_id=task.id,
            gate_type="review_order",
            status="pending",
            actor="@operator",
            mode="supervised",
            idempotency_key="done-terminal-pending",
            input_hash="done-terminal",
            input_payload={},
        ),
    ])
    db_session.commit()

    orchestration.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="done-terminal-verdict",
    )

    decision = (
        db_session.query(GateRecord)
        .filter(
            GateRecord.task_id == task.id,
            GateRecord.parent_id.isnot(None),
        )
        .one()
    )
    assert task.status == "done"
    assert decision.status == "rejected"
    assert decision.actor == "system:terminal-cleanup"
    assert task.awaiting_approval is False


@pytest.mark.parametrize(
    ("error_code", "details"),
    [
        (
            "schema_validation",
            {
                "code": "schema_validation",
                "path": "/repo/.ct/review.json",
                "details": {
                    "errors": [
                        {
                            "type": "extra_forbidden",
                            "loc": ["unexpected"],
                            "msg": "Extra inputs are not permitted",
                            "input": "value",
                        }
                    ]
                },
            },
        ),
        (
            "acceptance_criteria_count_mismatch",
            {
                "code": "acceptance_criteria_count_mismatch",
                "path": "/repo/.ct/review.json",
                "details": {"expected": 2, "actual": 1},
            },
        ),
    ],
)
def test_review_result_failure_preserves_executor_result_and_allows_new_reviewer(
    orchestration, db_session, error_code, details
):
    db_session.add_all(
        [
            Agent(id="@old-reviewer", name="Old", role="reviewer", cli="claude"),
            Agent(id="@new-reviewer", name="New", role="reviewer", cli="codex"),
        ]
    )
    task = _task(db_session, f"REVIEW-FAIL-{error_code}", mode="bypass")
    task.status = "in-review"
    task.executor = "@executor"
    task.reviewer = "@old-reviewer"
    task.result_ref = "base..head"
    starting_version = task.version
    db_session.commit()

    failure = orchestration.record_review_failure(
        task_id=task.id,
        error="Review result could not be loaded",
        actor="agent:@old-reviewer",
        idempotency_key=f"review-failure:{error_code}",
        run_id="failed-review-run",
        error_details=details,
    )

    assert failure.task.status == "awaiting-review"
    assert failure.task.version == starting_version + 1
    assert failure.task.result_ref == "base..head"
    assert failure.task.executor == "@executor"
    assert failure.task.awaiting_approval is False
    assert failure.gate_record.input_payload["error_details"] == details
    assert failure.gate_record.status == "rejected"

    reassigned = orchestration.request_review(
        task_id=task.id,
        reviewer="@new-reviewer",
        actor="@operator",
        idempotency_key=f"review-retry:{error_code}",
    )

    assert reassigned.task.status == "in-review"
    assert reassigned.task.result_ref == "base..head"
    assert reassigned.task.error is None
    assert reassigned.agent_run.kind == "review"
    assert reassigned.agent_run.agent_id == "@new-reviewer"
    assert reassigned.gate_record.reviewer == "@new-reviewer"
    assert reassigned.gate_record.executor == "@executor"


def test_orchestration_review_key_includes_reviewer_for_same_round(
    orchestration, db_session, monkeypatch
):
    """A legal reviewer replacement must not collide on task+round alone."""
    db_session.add_all(
        [
            Agent(id="@reviewer-a", name="Reviewer A", role="reviewer", cli="codex"),
            Agent(id="@reviewer-b", name="Reviewer B", role="reviewer", cli="codex"),
        ]
    )
    task = _task(db_session, "REVIEW-KEY-001", mode="supervised")
    task.status = "awaiting-review"
    task.executor = "@executor"
    task.result_ref = "base..head"
    db_session.commit()

    def candidate(agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            final_score=0.8,
            matched_skills=[],
            performance=0.8,
            rejection_reason=None,
        )

    first = SimpleNamespace(
        suggestions=[candidate("@reviewer-a")],
        candidates=[candidate("@reviewer-a")],
    )
    second = SimpleNamespace(
        suggestions=[candidate("@reviewer-b")],
        candidates=[candidate("@reviewer-b")],
    )

    import app.workers.agent_runner as runner

    scores = iter([first, second])
    monkeypatch.setattr(
        runner.AgentMatcher,
        "score_candidates",
        lambda *args, **kwargs: next(scores),
    )
    service = orchestration
    monkeypatch.setattr(
        service,
        "check_brakes",
        lambda *args, **kwargs: SimpleNamespace(allowed=True),
    )
    assert runner._advance_awaiting_review(db_session, service, task) == "gate_pending"
    db_session.refresh(task)
    assert runner._advance_awaiting_review(db_session, service, task) == "gate_pending"

    roots = (
        db_session.query(GateRecord)
        .filter(
            GateRecord.task_id == task.id,
            GateRecord.gate_type == "review_order",
            GateRecord.parent_id.is_(None),
        )
        .order_by(GateRecord.id)
        .all()
    )
    assert roots[0].input_payload["reviewer"] == "@reviewer-a"
    assert roots[1].input_payload["reviewer"] == "@reviewer-b"
    assert roots[0].idempotency_key != roots[1].idempotency_key
def test_explicit_review_cancellation_uses_review_failure_transition(
    orchestration, db_session
):
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    task = _task(db_session, "REVIEW-CANCEL", mode="bypass")
    task.status = "in-review"
    task.executor = "@executor"
    task.reviewer = "@reviewer"
    task.result_ref = "base..head"
    run = AgentRun(
        id="review-cancel-run",
        task_id=task.id,
        agent_id="@reviewer",
        cli="codex",
        command="codex review",
        kind="review",
        agent_role="reviewer",
        status="running",
    )
    db_session.add(run)
    db_session.commit()

    result = orchestration.cancel_run(
        run_id=run.id,
        actor="@operator",
        idempotency_key="cancel-review-run",
    )

    assert result.agent_run is run
    assert run.status == "cancelled"
    assert result.task.status == "awaiting-review"
    assert result.task.current_gate == "review_order"
    assert result.task.error == "Cancelled by user"
    assert result.gate_record.gate_type == "review_result"
    assert result.gate_record.status == "rejected"
    assert result.gate_record.output_ref == run.id

    replay = orchestration.cancel_run(
        run_id=run.id,
        actor="@operator",
        idempotency_key="cancel-review-run",
    )
    assert replay.task.status == "awaiting-review"
    assert db_session.query(GateRecord).filter_by(
        task_id=task.id, idempotency_key="cancel-review-run"
    ).count() == 1


def test_awaiting_approval_sync_ignores_resolved_pending_root(
    orchestration, db_session
):
    task = _task(db_session, "GATE-STALE-003")
    task.awaiting_approval = True
    pending = GateRecord(
        task_id=task.id,
        gate_type="dispatch",
        status="pending",
        actor="@operator",
        mode="supervised",
        idempotency_key="resolved-pending-root",
        input_hash="resolved",
        input_payload={},
    )
    db_session.add(pending)
    db_session.flush()
    db_session.add(
        GateRecord(
            task_id=task.id,
            gate_type="dispatch",
            status="rejected",
            actor="system:stale-cleanup",
            mode="supervised",
            idempotency_key="resolved-pending-root:terminal",
            input_hash="resolved",
            parent_id=pending.id,
            input_payload={},
        )
    )
    db_session.commit()

    assert orchestration.state_machine.sync_awaiting_approval(task) is False
    assert task.awaiting_approval is False


def test_failed_task_can_be_redispatched_after_gate_cleanup(orchestration, db_session):
    task = _task(db_session, "GATE-STALE-004")
    task.status = "dispatched"
    task.executor = "@executor"
    task.awaiting_approval = True
    db_session.add(
        GateRecord(
            task_id=task.id,
            gate_type="review_order",
            status="pending",
            actor="@operator",
            mode="supervised",
            idempotency_key="redispatch-stale-review",
            input_hash="redispatch",
            input_payload={},
        )
    )
    db_session.commit()

    orchestration.record_execution_failure(
        task_id=task.id,
        error="executor failed",
        actor="@executor",
        idempotency_key="redispatch-failure",
    )

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="redispatch-after-failure",
    )

    assert result.status == "pending"
    assert task.status == "failed"
    assert task.awaiting_approval is True


def test_supervised_dispatch_commits_pending_and_resumes_separately(
    orchestration,
    db_session,
):
    task = _task(db_session, "GATE-001")

    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-1",
    )

    assert pending.status == "pending"
    assert pending.applied is False
    assert pending.agent_run is None
    assert pending.task.status == "todo"
    assert pending.task.awaiting_approval is True
    assert db_session.query(AgentRun).count() == 0

    approved = orchestration.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="dispatch-1:approval",
    )

    assert approved.status == "approved"
    assert approved.applied is True
    assert approved.task.status == "dispatched"
    assert approved.task.executor == "@executor"
    assert approved.agent_run is not None
    assert approved.gate_record.parent_id == pending.gate_record.id


def test_bypass_dispatch_is_audited_and_idempotent(orchestration, db_session):
    task = _task(db_session, "GATE-002", mode="bypass")
    request = dict(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-bypass",
    )

    first = orchestration.request_dispatch(**request)
    second = orchestration.request_dispatch(**request)

    assert first.status == "approved"
    assert first.agent_run.id == second.agent_run.id
    assert db_session.query(AgentRun).count() == 1
    assert (
        db_session.query(GateRecord)
        .filter(GateRecord.idempotency_key == "dispatch-bypass")
        .count()
        == 1
    )


def test_dispatch_re_resolves_supervised_task_after_policy_changes_to_auto(
    orchestration,
    db_session,
):
    task = _task(db_session, "GATE-003", mode="supervised")
    task.risk = "normal"
    db_session.add(Setting(key="autonomy", value="auto"))
    db_session.add(Setting(key="auto_max_risk", value="normal"))
    db_session.commit()
    assert task.mode == "supervised"

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-after-auto-policy",
    )

    assert result.status == "approved"
    assert result.applied is True
    assert result.agent_run is not None
    assert result.task.awaiting_approval is False
    assert db_session.query(AgentRun).count() == 1


def test_dispatch_effort_override_takes_precedence_over_agent_default(
    orchestration, db_session
):
    db_session.query(Agent).filter(Agent.id == "@executor").update({"effort": "low"})
    db_session.commit()
    task = _task(db_session, "GATE-EFFORT-1", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-effort-override",
        effort="high",
    )

    assert result.agent_run.effort == "high"
    assert "model_reasoning_effort=high" in result.agent_run.command


def test_dispatch_effort_falls_back_to_agent_default(orchestration, db_session):
    db_session.query(Agent).filter(Agent.id == "@executor").update({"effort": "low"})
    db_session.commit()
    task = _task(db_session, "GATE-EFFORT-2", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-effort-default",
    )

    assert result.agent_run.effort == "low"


def test_dispatch_effort_defaults_to_medium_when_unset(orchestration, db_session):
    task = _task(db_session, "GATE-EFFORT-3", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-effort-medium",
    )

    assert result.agent_run.effort == "medium"


def test_autonomy_disabled_blocks_dispatch_and_is_audited(orchestration, db_session):
    task = _task(db_session, "GATE-BRAKE-1", mode="bypass")
    db_session.add(Setting(key="autonomy_enabled", value=False))
    db_session.commit()

    with pytest.raises(BrakeViolationError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-killed",
        )

    assert db_session.query(AgentRun).count() == 0
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "brake:autonomy_disabled")
        .one()
    )
    assert audit.task_id == task.id


def test_cost_cap_exceeded_stops_task_and_escalates(orchestration, db_session):
    task = _task(db_session, "GATE-BRAKE-2", mode="bypass")
    db_session.add(Setting(key="max_cost_usd_per_task", value="1.0"))
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="test-model",
            provider="test",
            operation="chat",
            cost_usd="5.0",
        )
    )
    db_session.commit()

    with pytest.raises(BrakeViolationError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-over-budget",
        )

    assert db_session.query(AgentRun).count() == 0
    db_session.refresh(task)
    assert task.status == "failed"
    assert task.awaiting_approval is False
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "brake:cost_limit")
        .one()
    )
    assert audit.task_id == task.id


def test_cli_subscription_cost_does_not_trip_usd_brake_but_tokens_do(
    orchestration, db_session
):
    task = _task(db_session, "GATE-BRAKE-TOKENS", mode="bypass")
    db_session.add(Setting(key="max_cost_usd_per_task", value="1.0"))
    db_session.add(Setting(key="max_tokens_per_task", value="10"))
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="claude-sonnet-5",
            provider="claude",
            operation="cli",
            input_tokens=7,
            cached_tokens=5,
            output_tokens=4,
            cost_usd="100.0",
        )
    )
    db_session.commit()

    with pytest.raises(BrakeViolationError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-over-token-budget",
        )

    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "brake:token_limit")
        .one()
    )
    assert audit.task_id == task.id


def test_plan_local_token_limit_tightens_global_brake(orchestration, db_session):
    task = _task(db_session, "GATE-PLAN-TOKENS", mode="bypass")
    task.limits = {"max_execution_rounds": 2, "max_tokens": 10}
    db_session.add(Setting(key="max_tokens_per_task", value="1000"))
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="test-model",
            provider="test",
            operation="chat",
            input_tokens=8,
            output_tokens=3,
            cost_usd="0",
        )
    )
    db_session.commit()

    with pytest.raises(BrakeViolationError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-over-plan-token-budget",
        )

    audit = db_session.query(AuditLog).filter(
        AuditLog.action == "brake:token_limit"
    ).one()
    assert audit.details["decision"]["observations"]["token_limit"] == 10


def test_dependency_pending_brake_queues_dispatch_instead_of_raising(
    orchestration, db_session
):
    """A queueable brake (dependency_pending) must not raise (CTV2-208 round 2).

    Only the terminal brakes (autonomy_disabled, cost_limit) should abort the
    dispatch outright; a queueable brake should let the AgentRun land as
    "queued" so the worker's own brake re-check (agent_runner.run_agent) can
    retry once the dependency clears.
    """
    blocker = _task(db_session, "GATE-BRAKE-DEP-1", mode="bypass")
    task = _task(db_session, "GATE-BRAKE-DEP-2", mode="bypass")
    db_session.add(TaskDependency(task_id=task.id, depends_on_task_id=blocker.id))
    db_session.commit()

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-dependency-pending",
    )

    assert result.agent_run is not None
    assert result.agent_run.status == "queued"
    db_session.refresh(task)
    assert task.status == "dispatched"
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "brake:dependency_pending")
        .one()
    )
    assert audit.task_id == task.id


def test_account_health_brake_queues_dispatch_instead_of_raising(
    orchestration, db_session
):
    """A queueable brake (account_health) must not raise (CTV2-208 round 2)."""
    db_session.add(
        AgentAccount(
            agent_id="@executor",
            cli="codex",
            status="cooldown",
            health_score=0.0,
        )
    )
    task = _task(db_session, "GATE-BRAKE-ACC-1", mode="bypass")
    db_session.commit()

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-account-unhealthy",
    )

    assert result.agent_run is not None
    assert result.agent_run.status == "queued"
    db_session.refresh(task)
    assert task.status == "dispatched"
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "brake:account_health")
        .one()
    )
    assert audit.task_id == task.id


def test_allowed_brake_decision_is_not_audited(orchestration, db_session):
    """CTV2-208 round 2: an allowed decision (code=None) must not spam the
    audit log with a "brake:None" entry — only violations are logged."""
    task = _task(db_session, "GATE-BRAKE-OK-1", mode="bypass")
    db_session.commit()

    orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-allowed",
    )

    assert (
        db_session.query(AuditLog).filter(AuditLog.action == "brake:None").count()
        == 0
    )
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.action.like("brake:%"))
        .count()
        == 0
    )


def test_idempotency_key_cannot_be_reused_with_new_input(
    orchestration,
    db_session,
):
    task = _task(db_session, "GATE-003")
    orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="same-key",
    )

    with pytest.raises(IdempotencyConflictError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="same-key",
            timeout_seconds=60,
        )


def test_gate_records_are_append_only(orchestration, db_session):
    task = _task(db_session, "GATE-004")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="immutable",
    )

    pending.gate_record.status = "approved"
    with pytest.raises((ValueError, StatementError), match="append-only"):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("mode", ["bypass", "supervised"])
def test_reusing_idempotency_key_after_run_goes_terminal_is_rejected(
    orchestration,
    db_session,
    mode,
):
    """Regression for CTV2-088: a stale dispatch record must never be
    replayed as `applied=True` once its AgentRun has left queued/running,
    even though the task's own status has since reverted to a state that
    would otherwise pass `_assert_status` (e.g. after a queue failure).

    Parametrized over both modes (round 3): under `supervised`, the record
    cached at the dispatch idempotency key is the *pending* parent, not the
    approved child that holds the run — a guard that only inspects the
    parent's status never fires, which is how this regression stayed green
    for two rounds."""
    task = _task(db_session, f"GATE-STALE-{mode.upper()}", mode=mode)

    first = _dispatch_and_approve(orchestration, task, "stale-key")
    assert first.applied is True
    run = first.agent_run
    assert run is not None

    # Simulate the run exhausting all its retries (e.g. the queue failed to
    # accept it) and the task being reset to "todo" so it once again
    # satisfies expected_status. `attempt >= max_attempts` is what makes a
    # "failed" run actually terminal (round 2 / AC3) — the worker retries a
    # failed run in place otherwise, so it would still be in flight.
    run.status = "failed"
    run.attempt = run.max_attempts
    task.status = "todo"
    db_session.commit()

    with pytest.raises(StaleIdempotencyRecordError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="stale-key",
        )

    # No new run was silently created, and the dead run is still the only one.
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


@pytest.mark.parametrize("mode", ["bypass", "supervised"])
def test_reusing_idempotency_key_after_failed_run_with_retries_left_is_not_stale(
    orchestration,
    db_session,
    mode,
):
    """AC3 nuance: a "failed" run that hasn't exhausted `max_attempts` is not
    terminal — the worker still retries it in place — so the cached record
    must keep being replayed as `applied=True`, not rejected as stale.
    Parametrized over both modes (round 3): see the terminal-run regression
    test above for why `supervised` needs its own coverage."""
    task = _task(db_session, f"GATE-RETRYABLE-{mode.upper()}", mode=mode)

    first = _dispatch_and_approve(orchestration, task, "retryable-key")
    run = first.agent_run
    run.status = "failed"
    run.attempt = 1
    run.max_attempts = 3
    db_session.commit()

    replay = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="retryable-key",
    )
    assert replay.applied is True
    assert replay.agent_run.id == run.id
    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


@pytest.mark.parametrize("mode", ["bypass", "supervised"])
@pytest.mark.parametrize("terminal_status", ["success", "timeout", "cancelled"])
def test_reusing_idempotency_key_after_run_completes_naturally_is_rejected(
    orchestration,
    db_session,
    terminal_status,
    mode,
):
    """Regression for CTV2-088 round 2 (missing coverage flagged by
    reviewer): the only stale-record test before this covered a queue-failure
    style "failed" run. A dispatch idempotency key reused after the run
    finished on its own — success, timeout, or cancellation — must be
    rejected the same way, never silently replayed as `applied=True`.
    Parametrized over both modes (round 3): see the terminal-run regression
    test above for why `supervised` needs its own coverage."""
    task = _task(
        db_session, f"GATE-STALE-{terminal_status.upper()}-{mode.upper()}", mode=mode
    )

    first = _dispatch_and_approve(orchestration, task, "stale-natural-key")
    run = first.agent_run
    run.status = terminal_status
    # Whatever status the task ended up in, put it back to "todo" so only
    # the run-staleness guard (not `_assert_status`) is what would reject a
    # naive replay here.
    task.status = "todo"
    db_session.commit()

    with pytest.raises(StaleIdempotencyRecordError):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="stale-natural-key",
        )

    assert db_session.query(AgentRun).filter(AgentRun.task_id == task.id).count() == 1


def test_decide_gate_idempotent_retry_preserves_dispatch_context(orchestration, db_session):
    task = _task(db_session, "GATE-005")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-5",
    )

    decide_args = dict(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="dispatch-5:approval",
    )

    first = orchestration.decide_gate(**decide_args)
    second = orchestration.decide_gate(**decide_args)

    assert first.status == "approved"
    assert second.status == "approved"
    assert first.agent_run.id == second.agent_run.id
    assert second.context is not None
    assert second.context.get("repo_root") == "/tmp"


@pytest.mark.parametrize("terminal_status", ["success", "failed", "timeout", "cancelled"])
def test_decide_gate_replay_after_run_goes_terminal_still_returns_cached_decision(
    orchestration,
    db_session,
    terminal_status,
):
    """Regression for CTV2-088 round 2: round 1 applied the dispatch
    stale-record guard to `decide_gate` too, which made replaying an
    already-decided gate (e.g. a duplicate POST /gates/{id}/decision) raise
    `StaleIdempotencyRecordError` forever once the run it created finished —
    even a perfectly normal success. The approve decision is an immutable
    historical fact; its replay must keep returning applied=True with the
    original run, regardless of that run's later status."""
    task = _task(db_session, f"GATE-APPROVE-REPLAY-{terminal_status.upper()}")
    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key=f"dispatch-{terminal_status}",
    )

    decide_args = dict(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key=f"dispatch-{terminal_status}:approval",
    )
    first = orchestration.decide_gate(**decide_args)
    run = first.agent_run
    assert run is not None

    run.status = terminal_status
    if terminal_status == "failed":
        run.attempt = run.max_attempts
    db_session.commit()

    replay = orchestration.decide_gate(**decide_args)
    assert replay.applied is True
    assert replay.status == "approved"
    assert replay.agent_run.id == run.id




def test_dispatch_rejects_task_with_no_acceptance_criteria(orchestration, db_session):
    """CTV2-091 AC: a task with no AC (the spec/plan gate never ran) must
    never reach dispatch — this is the fake-done hole the gate closes."""
    task = Task(
        id="SPEC-NO-AC",
        project="project",
        title="No AC yet",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(PrerequisiteError, match="acceptance_criteria"):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-no-ac",
        )


def test_dispatch_allows_legacy_no_ac_task_without_acceptance_criteria(
    orchestration, db_session
):
    """CTV2-091 migration path: pre-existing backlog tasks flagged
    `legacy_no_ac` must not get stuck behind the new AC gate."""
    task = Task(
        id="SPEC-LEGACY",
        project="project",
        title="Pre-existing task",
        mode="bypass",
        acceptance_criteria=[],
        legacy_no_ac=True,
    )
    db_session.add(task)
    db_session.commit()

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-legacy",
    )
    assert result.task.status == "dispatched"


def _critic_contract(*, constraints=None, limits=None):
    return {
        "constraints": constraints or [],
        "evidence": [{
            "fact": "Relevant module exists",
            "source_type": "file",
            "source": "backend/app/example.py:1",
            "result": "module exists",
        }],
        "prior_art": [],
        "ruled_out": [],
        "limits": limits,
        "planner": "@planner",
        "critic": "@critic",
        "critic_verdict": "accept",
        "critic_findings": [],
        "critic_summary": "Evidence reproduced",
        "critic_tokens": 1200,
    }


def test_write_spec_plan_populates_task_and_opens_dispatch_gate(
    orchestration, db_session
):
    task = Task(
        id="SPEC-001",
        project="project",
        title="Needs a spec",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()

    updated = orchestration.write_spec_plan(
        task_id=task.id,
        actor="@coordinator",
        acceptance_criteria=["Endpoint returns 200", "Unit tests pass"],
        **_critic_contract(constraints=["Do not add a migration"]),
        plan="1. Add route. 2. Add tests.",
        files=["backend/app/api/foo.py", "unconfirmed/made_up.py *(chưa xác nhận)*"],
        tests=["backend/tests/test_foo.py"],
        risk="low",
        flows=["checkout"],
        spec_clarity="high",
        open_questions=[],
    )

    assert updated.acceptance_criteria == ["Endpoint returns 200", "Unit tests pass"]
    assert updated.plan == "1. Add route. 2. Add tests."
    assert updated.current_gate == "plan"
    assert updated.flows == ["checkout"]
    audit = (
        db_session.query(AuditLog)
        .filter(AuditLog.task_id == task.id, AuditLog.action == "spec_plan_generated")
        .one()
    )
    assert audit.details["acceptance_count"] == 2
    assert audit.details["review_criteria_count"] == 3

    # The AC gate is now open: dispatch no longer needs legacy_no_ac.
    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-after-spec-plan",
    )
    assert result.task.status == "dispatched"


def test_write_spec_plan_rejects_empty_acceptance_criteria(orchestration, db_session):
    task = Task(
        id="SPEC-002",
        project="project",
        title="Needs a spec",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()

    with pytest.raises(PrerequisiteError, match="acceptance_criteria"):
        orchestration.write_spec_plan(
            task_id=task.id,
            actor="@coordinator",
            acceptance_criteria=[],
            **_critic_contract(),
            plan="plan",
            files=[],
            tests=[],
            risk="low",
            flows=[],
            spec_clarity="high",
            open_questions=[],
        )


def test_constraints_only_plan_dispatches_and_critic_is_in_ledger(
    orchestration, db_session
):
    task = Task(
        id="SPEC-CONSTRAINTS",
        project="project",
        title="Boundary-only change",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()

    updated = orchestration.write_spec_plan(
        task_id=task.id,
        actor="@coordinator",
        acceptance_criteria=[],
        **_critic_contract(constraints=["Do not modify the public API"]),
        plan="Preserve the API while applying the internal fix.",
        files=[],
        tests=[],
        risk="low",
        flows=[],
        spec_clarity="high",
        open_questions=[],
    )
    critic_row = db_session.query(GateRecord).filter_by(
        task_id=task.id, gate_type="plan_critic"
    ).one()
    assert critic_row.status == "approved"
    assert critic_row.executor == "@planner"
    assert critic_row.reviewer == "@critic"
    assert critic_row.input_payload["diff_provided"] is False
    assert updated.constraints == ["Do not modify the public API"]

    dispatched = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="constraint-only-dispatch",
    )
    assert dispatched.task.status == "dispatched"


def test_rejected_plan_critic_blocks_dispatch_and_requires_evidence(
    orchestration, db_session
):
    task = Task(
        id="SPEC-REJECTED",
        project="project",
        title="Bad plan",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()
    rejected = _critic_contract()
    rejected.update({
        "critic_verdict": "reject",
        "critic_findings": [{
            "target": "evidence",
            "description": "The cited file does not exist",
            "evidence": ["test -e backend/app/example.py -> exit 1"],
        }],
        "critic_summary": "Planner evidence did not reproduce",
    })
    updated = orchestration.write_spec_plan(
        task_id=task.id,
        actor="@coordinator",
        acceptance_criteria=["Observable outcome"],
        **rejected,
        plan="Edit the missing file.",
        files=[],
        tests=[],
        risk="low",
        flows=[],
        spec_clarity="high",
        open_questions=[],
    )
    assert updated.plan_critic_status == "reject"
    assert updated.awaiting_approval is True
    assert db_session.query(GateRecord).filter_by(
        task_id=task.id, gate_type="plan_critic", status="rejected"
    ).count() == 1
    with pytest.raises(PrerequisiteError, match="independent plan critic"):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="must-not-dispatch",
        )

    invalid = _critic_contract()
    invalid.update({
        "critic_verdict": "reject",
        "critic_findings": [{"target": "contract", "description": "Vague"}],
    })
    with pytest.raises(PrerequisiteError, match="reproducible evidence"):
        orchestration.write_spec_plan(
            task_id=task.id,
            actor="@coordinator",
            acceptance_criteria=["Outcome"],
            **invalid,
            plan="Another plan",
            files=[], tests=[], risk="low", flows=[],
            spec_clarity="high", open_questions=[],
        )


def test_plan_critic_four_eyes_and_plan_edit_invalidation(orchestration, db_session):
    task = Task(
        id="SPEC-FOUR-EYES",
        project="project",
        title="Independent plan review",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()
    same_agent = _critic_contract()
    same_agent["critic"] = same_agent["planner"]
    with pytest.raises(PrerequisiteError, match="must differ"):
        orchestration.write_spec_plan(
            task_id=task.id, actor="@coordinator",
            acceptance_criteria=["Outcome"], **same_agent,
            plan="Plan", files=[], tests=[], risk="low", flows=[],
            spec_clarity="high", open_questions=[],
        )

    orchestration.write_spec_plan(
        task_id=task.id, actor="@coordinator",
        acceptance_criteria=["Outcome"], **_critic_contract(),
        plan="Accepted plan", files=[], tests=[], risk="low", flows=[],
        spec_clarity="high", open_questions=[],
    )
    version = task.version
    edited = orchestration.update_task_fields(
        task_id=task.id,
        patch={"plan": "Edited after criticism"},
        actor="@coordinator",
    )
    assert edited.version == version + 1
    assert edited.plan_critic_status is None
    with pytest.raises(PrerequisiteError, match="independent plan critic"):
        orchestration.request_dispatch(
            task_id=task.id, agent_id="@executor", actor="@operator",
            idempotency_key="stale-critic-verdict",
        )


def test_execute_dispatch_blocks_open_questions_until_clear(orchestration, db_session):
    task = Task(
        id="SPEC-QUESTIONS",
        project="project",
        title="Ambiguous spec",
        mode="bypass",
        acceptance_criteria=[],
    )
    db_session.add(task)
    db_session.commit()

    pending = orchestration.write_spec_plan(
        task_id=task.id,
        actor="@coordinator",
        acceptance_criteria=["Observable result"],
        **_critic_contract(),
        plan="Research and implement.",
        files=[],
        tests=[],
        risk="low",
        flows=[],
        spec_clarity="medium",
        open_questions=["Which authentication convention applies?"],
    )
    assert pending.awaiting_approval is True
    assert "1) Which authentication convention applies?" in pending.approval_prompt

    with pytest.raises(
        PrerequisiteError,
        match="Spec has 1 unanswered open questions",
    ):
        orchestration.request_dispatch(
            task_id=task.id,
            agent_id="@executor",
            actor="@operator",
            idempotency_key="dispatch-with-question",
        )

    cleared = orchestration.write_spec_plan(
        task_id=task.id,
        actor="@coordinator",
        acceptance_criteria=["Observable result"],
        **_critic_contract(),
        plan="Research and implement.",
        files=[],
        tests=[],
        risk="low",
        flows=[],
        spec_clarity="high",
        open_questions=[],
    )
    assert cleared.open_questions == []
    assert cleared.awaiting_approval is False
    assert cleared.approval_prompt is None

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="dispatch-after-questions-clear",
    )
    assert result.task.status == "dispatched"


def test_reopen_for_replan_transitions_changes_requested_to_todo(orchestration, db_session):
    task = _task(db_session, "REPLAN-001", mode="bypass")
    task.status = "changes-requested"
    task.verdict = "changes"
    db_session.commit()

    result = orchestration.reopen_for_replan(
        task_id=task.id,
        actor="system:orchestration-driver",
        idempotency_key="replan-1",
    )

    assert result.applied is True
    assert result.task.status == "todo"
    assert result.task.current_gate == "plan"
    assert result.task.verdict is None
    assert result.gate_record.gate_type == "replan"
    assert orchestration.changes_round_count(task.id) == 0


def test_reopen_for_replan_requires_changes_requested_status(orchestration, db_session):
    task = _task(db_session, "REPLAN-002", mode="bypass")

    with pytest.raises(TransitionConflictError):
        orchestration.reopen_for_replan(
            task_id=task.id,
            actor="system:orchestration-driver",
            idempotency_key="replan-2",
        )


def test_reopen_for_replan_is_idempotent(orchestration, db_session):
    task = _task(db_session, "REPLAN-003", mode="bypass")
    task.status = "changes-requested"
    db_session.commit()

    first = orchestration.reopen_for_replan(
        task_id=task.id,
        actor="system:orchestration-driver",
        idempotency_key="replan-3",
    )
    # A same-key replay after the task has already moved on to "todo" must
    # return the cached record rather than asserting the old pre-state.
    second = orchestration.reopen_for_replan(
        task_id=task.id,
        actor="system:orchestration-driver",
        idempotency_key="replan-3",
    )

    assert first.gate_record.id == second.gate_record.id
    assert orchestration.changes_round_count(task.id) == 0


def test_changes_round_count_uses_highest_task_round_number(
    orchestration, db_session
):
    task = _task(db_session, "REPLAN-004", mode="bypass")
    assert orchestration.changes_round_count(task.id) == 0

    db_session.add(
        GateRecord(
            task_id=task.id,
            gate_type="replan",
            status="approved",
            actor="@driver",
            idempotency_key="replan-4a",
            input_hash="hash-4a",
        )
    )
    db_session.add(TaskRound(task_id=task.id, round_no=1))
    db_session.add(TaskRound(task_id=task.id, round_no=3))
    db_session.commit()
    assert orchestration.changes_round_count(task.id) == 3



def test_write_spec_plan_requires_todo_status(orchestration, db_session):
    task = _task(db_session, "SPEC-003")
    task.status = "dispatched"
    db_session.commit()

    with pytest.raises(TransitionConflictError):
        orchestration.write_spec_plan(
            task_id=task.id,
            actor="@coordinator",
            acceptance_criteria=["AC"],
            **_critic_contract(),
            plan="plan",
            files=[],
            tests=[],
            risk="low",
            flows=[],
            spec_clarity="high",
            open_questions=[],
        )


# ---------------------------------------------------------------------------
# task_dependencies: add_dependency, cycle rejection, wake_dependents (CTV2-094)
# ---------------------------------------------------------------------------


def test_add_dependency_records_edge(orchestration, db_session):
    upstream = _task(db_session, "DEP-001")
    downstream = _task(db_session, "DEP-002")

    edge = orchestration.add_dependency(
        task_id=downstream.id, depends_on_task_id=upstream.id, actor="@operator"
    )

    assert edge.task_id == "DEP-002"
    assert edge.depends_on_task_id == "DEP-001"
    assert (
        db_session.query(TaskDependency)
        .filter(TaskDependency.task_id == "DEP-002")
        .count()
        == 1
    )
    assert (
        db_session.query(AuditLog)
        .filter(AuditLog.task_id == "DEP-002", AuditLog.action == "add_dependency")
        .count()
        == 1
    )


def test_add_dependency_is_idempotent(orchestration, db_session):
    upstream = _task(db_session, "DEP-003")
    downstream = _task(db_session, "DEP-004")

    first = orchestration.add_dependency(
        task_id=downstream.id, depends_on_task_id=upstream.id, actor="@operator"
    )
    second = orchestration.add_dependency(
        task_id=downstream.id, depends_on_task_id=upstream.id, actor="@operator"
    )

    assert first.task_id == second.task_id
    assert (
        db_session.query(TaskDependency)
        .filter(TaskDependency.task_id == "DEP-004")
        .count()
        == 1
    )


def test_add_dependency_rejects_self_reference(orchestration, db_session):
    task = _task(db_session, "DEP-005")

    with pytest.raises(DependencyCycleError):
        orchestration.add_dependency(
            task_id=task.id, depends_on_task_id=task.id, actor="@operator"
        )


def test_add_dependency_rejects_direct_cycle(orchestration, db_session):
    a = _task(db_session, "DEP-006")
    b = _task(db_session, "DEP-007")
    orchestration.add_dependency(task_id=b.id, depends_on_task_id=a.id, actor="@op")

    with pytest.raises(DependencyCycleError):
        orchestration.add_dependency(task_id=a.id, depends_on_task_id=b.id, actor="@op")


def test_add_dependency_rejects_transitive_cycle(orchestration, db_session):
    a = _task(db_session, "DEP-008")
    b = _task(db_session, "DEP-009")
    c = _task(db_session, "DEP-010")
    # C depends on B, B depends on A -- A -> C would close the loop.
    orchestration.add_dependency(task_id=c.id, depends_on_task_id=b.id, actor="@op")
    orchestration.add_dependency(task_id=b.id, depends_on_task_id=a.id, actor="@op")

    with pytest.raises(DependencyCycleError):
        orchestration.add_dependency(task_id=a.id, depends_on_task_id=c.id, actor="@op")


def test_add_dependency_requires_existing_tasks(orchestration, db_session):
    task = _task(db_session, "DEP-011")

    with pytest.raises(TaskNotFoundError):
        orchestration.add_dependency(
            task_id=task.id, depends_on_task_id="MISSING", actor="@op"
        )
    with pytest.raises(TaskNotFoundError):
        orchestration.add_dependency(
            task_id="MISSING", depends_on_task_id=task.id, actor="@op"
        )


def test_unmet_and_failed_dependencies(orchestration, db_session):
    upstream = _task(db_session, "DEP-012")
    downstream = _task(db_session, "DEP-013")
    orchestration.add_dependency(
        task_id=downstream.id, depends_on_task_id=upstream.id, actor="@op"
    )

    assert [t.id for t in orchestration.unmet_dependencies(downstream.id)] == ["DEP-012"]
    assert orchestration.failed_dependencies(downstream.id) == []

    upstream = db_session.get(Task, upstream.id)
    upstream.status = "failed"
    db_session.commit()
    assert orchestration.failed_dependencies(downstream.id) == ["DEP-012"]

    upstream = db_session.get(Task, upstream.id)
    upstream.executor = "@executor"
    upstream.reviewer = "@reviewer"
    upstream.result_ref = "base..head"
    upstream.status = "done"
    db_session.commit()
    assert orchestration.unmet_dependencies(downstream.id) == []
    assert orchestration.failed_dependencies(downstream.id) == []


def test_wake_dependents_sends_advance_task_for_every_dependent(orchestration, db_session):
    upstream = _task(db_session, "DEP-014")
    downstream_a = _task(db_session, "DEP-015")
    downstream_b = _task(db_session, "DEP-016")
    orchestration.add_dependency(
        task_id=downstream_a.id, depends_on_task_id=upstream.id, actor="@op"
    )
    orchestration.add_dependency(
        task_id=downstream_b.id, depends_on_task_id=upstream.id, actor="@op"
    )

    with patch("app.workers.agent_runner.advance_task") as driver_actor:
        orchestration.wake_dependents(upstream.id)

    sent = {call.args[0] for call in driver_actor.send.call_args_list}
    assert sent == {"DEP-015", "DEP-016"}


def test_wake_dependents_is_a_noop_with_no_dependents(orchestration, db_session):
    task = _task(db_session, "DEP-017")

    with patch("app.workers.agent_runner.advance_task") as driver_actor:
        orchestration.wake_dependents(task.id)

    driver_actor.send.assert_not_called()


def test_record_execution_failure_wakes_dependents(orchestration, db_session):
    upstream = _task(db_session, "DEP-018")
    downstream = _task(db_session, "DEP-019")
    orchestration.add_dependency(
        task_id=downstream.id, depends_on_task_id=upstream.id, actor="@op"
    )
    upstream.status = "dispatched"
    db_session.commit()

    with patch("app.workers.agent_runner.advance_task") as driver_actor:
        orchestration.record_execution_failure(
            task_id=upstream.id,
            error="boom",
            actor="agent:@executor",
            idempotency_key="dep-018-fail",
        )

    driver_actor.send.assert_called_once_with("DEP-019", "dependency_closed")


# ---------------------------------------------------------------------------
# Autonomy policy: Settings + Project override (CTV2-093)
# ---------------------------------------------------------------------------


def test_resolve_autonomy_defaults_and_fallbacks(orchestration, db_session):
    # Safe defaults when no setting or project override exists
    policy = orchestration.resolve_autonomy(None)
    assert policy.autonomy == "supervised"
    assert policy.auto_max_risk == "normal"
    assert policy.auto_max_rounds == 3

    # Invalid setting values fail-safe to defaults
    db_session.add(Setting(key="autonomy", value="INVALID"))
    db_session.add(Setting(key="auto_max_risk", value="CRITICAL"))
    db_session.add(Setting(key="auto_max_rounds", value="NOT_NUMERIC"))
    db_session.commit()

    policy = orchestration.resolve_autonomy(None)
    assert policy.autonomy == "supervised"
    assert policy.auto_max_risk == "normal"
    assert policy.auto_max_rounds == 3


def test_resolve_autonomy_project_override_wins_global_setting(orchestration, db_session):
    db_session.add(Setting(key="autonomy", value="supervised"))
    db_session.add(Setting(key="auto_max_risk", value="low"))
    db_session.add(Setting(key="auto_max_rounds", value=2))

    project = Project(
        id="OVERRIDE-PROJ",
        name="Override Project",
        autonomy_policy={
            "autonomy": "auto",
            "auto_max_risk": "normal",
            "auto_max_rounds": 5,
        },
    )
    db_session.add(project)
    db_session.commit()

    policy = orchestration.resolve_autonomy(project)
    assert policy.autonomy == "auto"
    assert policy.auto_max_risk == "normal"
    assert policy.auto_max_rounds == 5

    # Test passing project ID string
    policy_by_id = orchestration.resolve_autonomy("OVERRIDE-PROJ")
    assert policy_by_id.autonomy == "auto"


def test_mode_for_task_matrix(orchestration, db_session):
    # Global policy: auto, max_risk: normal
    db_session.add(Setting(key="autonomy", value="auto"))
    db_session.add(Setting(key="auto_max_risk", value="normal"))
    db_session.commit()

    proj = Project(id="MATRIX-PROJ", name="Matrix")
    db_session.add(proj)
    db_session.commit()

    task_low = _task(db_session, "MAT-001", mode="supervised")
    task_low.project = "MATRIX-PROJ"
    task_low.risk = "low"

    task_normal = _task(db_session, "MAT-002", mode="supervised")
    task_normal.project = "MATRIX-PROJ"
    task_normal.risk = "normal"

    task_high = _task(db_session, "MAT-003", mode="supervised")
    task_high.project = "MATRIX-PROJ"
    task_high.risk = "high"

    # risk low/normal <= auto_max_risk normal -> bypass
    assert orchestration.mode_for_task(task_low) == "bypass"
    assert orchestration.mode_for_task(task_normal) == "bypass"
    # risk high > auto_max_risk normal -> supervised
    assert orchestration.mode_for_task(task_high) == "supervised"

    # Project override autonomy=plan-only
    proj.autonomy_policy = {"autonomy": "plan-only"}
    db_session.commit()
    assert orchestration.mode_for_task(task_low) == "plan-only"
    assert orchestration.mode_for_task(task_high) == "plan-only"


def test_write_spec_plan_updates_task_mode_by_policy(orchestration, db_session):
    db_session.add(Setting(key="autonomy", value="auto"))
    db_session.add(Setting(key="auto_max_risk", value="normal"))
    db_session.commit()

    task_low = _task(db_session, "SPEC-MODE-001", mode="supervised")
    orchestration.write_spec_plan(
        task_id=task_low.id,
        actor="@planner",
        acceptance_criteria=["AC1"],
        **_critic_contract(),
        plan="Plan",
        files=[],
        tests=[],
        risk="low",
        flows=[],
        spec_clarity="high",
        open_questions=[],
    )
    db_session.refresh(task_low)
    assert task_low.mode == "bypass"

    task_high = _task(db_session, "SPEC-MODE-002", mode="supervised")
    orchestration.write_spec_plan(
        task_id=task_high.id,
        actor="@planner",
        acceptance_criteria=["AC1"],
        **_critic_contract(limits={
            "max_execution_rounds": 2,
            "max_tokens": 100_000,
            "max_cost_usd": 5.0,
        }),
        plan="Plan",
        files=[],
        tests=[],
        risk="high",
        flows=[],
        spec_clarity="high",
        open_questions=[],
    )
    db_session.refresh(task_high)
    assert task_high.mode == "supervised"


def test_dispatch_creates_task_round_and_links_it_to_task(orchestration, db_session):
    task = _task(db_session, "ROUND-001", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="round-dispatch-1",
    )

    assert result.applied is True
    assert result.task.current_round_id is not None
    round_ = db_session.get(TaskRound, result.task.current_round_id)
    assert round_ is not None
    assert round_.task_id == task.id
    assert round_.round_no == 1
    assert round_.status == "dispatched"
    assert round_.executor_agent_id == "@executor"
    assert round_.executor_run_id == result.agent_run.id
    assert round_.started_at is not None


def test_request_dispatch_changes_requested_seeds_prior_result_ref(
    orchestration, db_session
):
    task = _task(db_session, "REDISPATCH-001", mode="bypass")
    task.status = "changes-requested"
    task.result_ref = "task-base..task-head"
    db_session.add(
        TaskRound(
            task_id=task.id,
            round_no=1,
            result_ref="round-base..prior-head",
            status="changes-requested",
        )
    )
    task.findings = [
        {
            "file": "app/example.py",
            "line": 7,
            "severity": "medium",
            "description": "Handle the empty input case.",
        }
    ]
    task.verdict = "changes"
    db_session.commit()

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="redispatch-1",
    )

    assert result.applied is True
    assert result.agent_run.result_ref == "prior-head..prior-head"
    outbox = next(
        event
        for event in db_session.query(OutboxEvent).all()
        if event.payload.get("run_id") == result.agent_run.id
    )
    assert outbox.payload["task_id"] == task.id


def test_todo_dispatch_does_not_seed_a_prior_result_ref(orchestration, db_session):
    task = _task(db_session, "REDISPATCH-002", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="redispatch-2",
    )

    assert result.agent_run.result_ref is None


def test_second_dispatch_after_replan_opens_a_second_round(orchestration, db_session):
    task = _task(db_session, "ROUND-002", mode="bypass")

    first = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="round-dispatch-2a",
    )
    first_round_id = first.task.current_round_id

    first.agent_run.status = "success"
    task.status = "changes-requested"
    db_session.commit()
    orchestration.reopen_for_replan(
        task_id=task.id, actor="@driver", idempotency_key="round-replan-2"
    )

    second = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="round-dispatch-2b",
    )

    assert second.task.current_round_id != first_round_id
    rounds = (
        db_session.query(TaskRound)
        .filter(TaskRound.task_id == task.id)
        .order_by(TaskRound.round_no)
        .all()
    )
    assert [r.round_no for r in rounds] == [1, 2]
    assert rounds[1].id == second.task.current_round_id


def test_verdict_pass_updates_current_round_and_task_projection_fields(
    orchestration, db_session
):
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    task = _task(db_session, "ROUND-003", mode="bypass")

    dispatched = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="round-dispatch-3",
    )
    round_id = dispatched.task.current_round_id

    db_session.add(
        AgentRun(
            id="round-3-review-run",
            task_id=task.id,
            agent_id="@reviewer",
            cli="codex",
            command="codex exec /code-review",
            kind="review",
            agent_role="reviewer",
            status="success",
        )
    )
    task.status = "in-review"
    task.reviewer = "@reviewer"
    task.result_ref = "base..head"
    db_session.commit()

    result = orchestration.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="round-verdict-3",
    )

    assert result.task.status == "done"
    assert result.task.final_result_ref == "base..head"
    assert result.task.final_verdict == "pass"

    round_ = db_session.get(TaskRound, round_id)
    assert round_.verdict == "pass"
    assert round_.status == "done"
    assert round_.reviewer_agent_id == "@reviewer"
    assert round_.reviewer_run_id == "round-3-review-run"
    assert round_.result_ref == "base..head"
    assert round_.completed_at is not None


def test_verdict_changes_updates_round_without_setting_final_projection(
    orchestration, db_session
):
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    task = _task(db_session, "ROUND-004", mode="bypass")

    dispatched = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="round-dispatch-4",
    )
    round_id = dispatched.task.current_round_id

    db_session.add(
        AgentRun(
            id="round-4-review-run",
            task_id=task.id,
            agent_id="@reviewer",
            cli="codex",
            command="codex exec /code-review",
            kind="review",
            agent_role="reviewer",
            status="success",
        )
    )
    task.status = "in-review"
    task.reviewer = "@reviewer"
    task.result_ref = "base..head"
    db_session.commit()

    result = orchestration.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="@reviewer",
        idempotency_key="round-verdict-4",
    )

    assert result.task.status == "changes-requested"
    assert result.task.final_result_ref is None
    assert result.task.final_verdict is None

    round_ = db_session.get(TaskRound, round_id)
    assert round_.verdict == "changes"
    assert round_.status == "changes-requested"


def test_verdict_without_a_current_round_does_not_crash(orchestration, db_session):
    """Tasks driven straight to in-review without going through
    request_dispatch (as several other tests in this suite do) have no
    current_round_id -- the verdict-round update must be a no-op, not an
    error."""
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    task = _task(db_session, "ROUND-005", mode="bypass")
    task.status = "in-review"
    task.executor = "@executor"
    task.reviewer = "@reviewer"
    task.result_ref = "base..head"
    db_session.commit()
    db_session.add(
        AgentRun(
            id="round-5-review-run",
            task_id=task.id,
            agent_id="@reviewer",
            cli="codex",
            command="codex exec /code-review",
            kind="review",
            agent_role="reviewer",
            status="success",
        )
    )
    db_session.commit()

    result = orchestration.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[{"passed": True}],
        actor="@reviewer",
        idempotency_key="round-verdict-5",
    )

    assert result.task.status == "done"
    assert result.task.current_round_id is None
    assert db_session.query(TaskRound).filter(TaskRound.task_id == task.id).count() == 0


# --- CTV2-204: task-level locking and idempotency -------------------------


def test_dispatch_bumps_task_version(orchestration, db_session):
    task = _task(db_session, "LOCK-001", mode="bypass")
    assert task.version == 0

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="lock-dispatch-1",
    )

    assert result.task.version == 1
    assert result.agent_run.idempotency_key == "lock-dispatch-1"
    assert result.agent_run.task_round_id == result.task.current_round_id


def test_cas_status_raises_when_version_changed_since_read(orchestration, db_session):
    """A caller holding a stale in-memory Task (older version) must get a
    hard conflict from `_cas_status`, not a silent overwrite -- this is the
    guard that protects backends without real row locks (e.g. SQLite)."""
    from sqlalchemy import text

    task = _task(db_session, "LOCK-002", mode="bypass")
    assert task.status == "todo"
    assert task.version == 0

    # Simulate another transaction having already advanced the row, via a
    # raw statement that bypasses ORM session sync so `task` keeps holding
    # the stale values a real concurrent transaction would have read.
    db_session.execute(
        text("UPDATE tasks SET status = 'dispatched', version = 1 WHERE id = :id"),
        {"id": task.id},
    )
    assert task.status == "todo"
    assert task.version == 0

    with pytest.raises(TransitionConflictError):
        orchestration._cas_status(task, "dispatched")

    row = db_session.execute(
        text("SELECT status, version FROM tasks WHERE id = :id"), {"id": task.id}
    ).one()
    assert row.status == "dispatched"
    assert row.version == 1


def test_agent_run_unique_round_kind_attempt(db_session):
    db_session.add(Project(id="proj-uniq", name="Uniq", repo_root="/tmp"))
    db_session.add(
        Task(
            id="LOCK-003",
            project="proj-uniq",
            title="Uniq task",
            status="dispatched",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.add(
        TaskRound(id="round-uniq", task_id="LOCK-003", round_no=1, status="dispatched")
    )
    db_session.commit()

    db_session.add(
        AgentRun(
            id="run-uniq-1",
            task_id="LOCK-003",
            task_round_id="round-uniq",
            agent_id="@executor",
            cli="codex",
            command="codex exec",
            kind="execute",
            attempt=1,
        )
    )
    db_session.commit()

    db_session.add(
        AgentRun(
            id="run-uniq-2",
            task_id="LOCK-003",
            task_round_id="round-uniq",
            agent_id="@executor",
            cli="codex",
            command="codex exec",
            kind="execute",
            attempt=1,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_agent_run_unique_task_idempotency_key(db_session):
    db_session.add(Project(id="proj-uniq2", name="Uniq2", repo_root="/tmp"))
    db_session.add(
        Task(
            id="LOCK-004",
            project="proj-uniq2",
            title="Uniq task 2",
            status="dispatched",
            acceptance_criteria=["Tests pass"],
        )
    )
    db_session.commit()

    db_session.add(
        AgentRun(
            id="run-idem-1",
            task_id="LOCK-004",
            agent_id="@executor",
            cli="codex",
            command="codex exec",
            idempotency_key="same-key",
        )
    )
    db_session.commit()

    db_session.add(
        AgentRun(
            id="run-idem-2",
            task_id="LOCK-004",
            agent_id="@executor",
            cli="codex",
            command="codex exec",
            idempotency_key="same-key",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_bypass_dispatch_creates_agent_run_and_outbox_event_atomically(
    orchestration, db_session
):
    """CTV2-205: the outbox row must land in the same commit as the run.

    Guards against the pre-outbox failure mode -- INSERT AgentRun -> COMMIT
    -> run_agent.send() -- where a crash between the commit and the send
    left a "queued" run with nothing to ever wake it up.
    """
    task = _task(db_session, "OUTBOX-001", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="outbox-dispatch-1",
    )

    assert result.applied is True
    run = result.agent_run
    assert run is not None

    events = db_session.query(OutboxEvent).filter(OutboxEvent.event_type == "run_requested").all()
    assert len(events) == 1
    event = events[0]
    assert event.published_at is None
    assert event.attempts == 0
    assert event.dead_letter is False
    assert event.payload["run_id"] == run.id
    assert event.payload["task_id"] == task.id
    assert event.payload["command"] == run.command
    assert event.payload["repo_root"] == "/tmp"


def test_supervised_dispatch_approval_creates_outbox_event_with_the_run(
    orchestration, db_session
):
    """The supervised path creates the AgentRun (and its outbox row) only
    once a human approves the pending gate, not at request time."""
    task = _task(db_session, "OUTBOX-002")

    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="outbox-dispatch-2",
    )
    assert db_session.query(OutboxEvent).count() == 0

    approved = orchestration.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="outbox-dispatch-2:approval",
    )

    events = db_session.query(OutboxEvent).all()
    assert len(events) == 1
    assert events[0].payload["run_id"] == approved.agent_run.id


def test_review_order_dispatch_creates_outbox_event(orchestration, db_session):
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.commit()
    task = _task(db_session, "OUTBOX-003", mode="bypass")
    dispatch = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="outbox-review-dispatch",
    )
    task.status = "awaiting-review"
    task.result_ref = "base..head"
    db_session.commit()

    review = orchestration.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key="outbox-review-order",
    )

    events = db_session.query(OutboxEvent).filter(OutboxEvent.event_type == "run_requested").all()
    run_ids = {event.payload["run_id"] for event in events}
    assert run_ids == {dispatch.agent_run.id, review.agent_run.id}
    db_session.rollback()


def test_bypass_dispatch_persists_dispatch_decision_and_candidates(
    orchestration, db_session
):
    db_session.add(
        Agent(id="@other-executor", name="Other Executor", role="executor", cli="codex")
    )
    db_session.commit()
    task = _task(db_session, "DECISION-001", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="decision-dispatch-1",
    )

    assert result.agent_run.dispatch_decision_id is not None
    decision = db_session.get(DispatchDecision, result.agent_run.dispatch_decision_id)
    assert decision is not None
    assert decision.task_id == task.id
    assert decision.kind == "execute"
    assert decision.selected_agent_id == "@executor"
    assert decision.policy_version
    assert decision.task_feature_snapshot is not None
    assert decision.exploration is False

    candidates = (
        db_session.query(DispatchCandidate)
        .filter(DispatchCandidate.dispatch_decision_id == decision.id)
        .all()
    )
    assert {c.agent_id for c in candidates} == {"@executor", "@other-executor"}
    assert all(c.eligible for c in candidates)
    assert all(c.final_score is not None for c in candidates)


def test_supervised_dispatch_persists_dispatch_decision_before_agent_run_exists(
    orchestration, db_session
):
    task = _task(db_session, "DECISION-002")

    pending = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="decision-dispatch-2",
    )

    assert pending.agent_run is None
    decisions = (
        db_session.query(DispatchDecision).filter(DispatchDecision.task_id == task.id).all()
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.selected_agent_id == "@executor"

    approved = orchestration.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="decision-dispatch-2:approval",
    )

    assert approved.agent_run.dispatch_decision_id == decision.id


def test_bypass_dispatch_idempotent_replay_reuses_dispatch_decision(
    orchestration, db_session
):
    task = _task(db_session, "DECISION-003", mode="bypass")
    request = dict(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="decision-dispatch-3",
    )

    first = orchestration.request_dispatch(**request)
    second = orchestration.request_dispatch(**request)

    assert first.agent_run.dispatch_decision_id == second.agent_run.dispatch_decision_id
    assert (
        db_session.query(DispatchDecision)
        .filter(DispatchDecision.task_id == task.id)
        .count()
        == 1
    )


def test_dispatch_decision_flags_human_override_when_selected_agent_is_not_top_ranked(
    orchestration, db_session
):
    db_session.add(
        Agent(
            id="@star-executor",
            name="Star Executor",
            role="executor",
            cli="codex",
            success_rate=1.0,
            effort="high",
        )
    )
    db_session.commit()
    task = _task(db_session, "DECISION-004", mode="bypass")

    result = orchestration.request_dispatch(
        task_id=task.id,
        agent_id="@executor",
        actor="@operator",
        idempotency_key="decision-dispatch-4",
    )

    decision = db_session.get(DispatchDecision, result.agent_run.dispatch_decision_id)
    assert decision.human_override is True


def test_check_brakes_pumps_updated_at_when_pid_is_alive(orchestration, db_session):
    import os
    from datetime import datetime, timedelta, timezone

    task = _task(db_session, "BRAKE-PID-1", mode="bypass")
    old_time = datetime.now(timezone.utc) - timedelta(seconds=500)
    run = AgentRun(
        id="run-pid-alive",
        task_id=task.id,
        agent_id="@executor",
        cli="claude",
        command="claude -p 'test'",
        status="running",
        started_at=old_time,
        updated_at=old_time,
        pid=os.getpid(),
    )
    db_session.add(run)
    db_session.commit()

    brake = orchestration.check_brakes(task, run_id=run.id, audit=True)

    assert brake.allowed is True
    db_session.refresh(run)
    updated_at = run.updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    assert updated_at > old_time + timedelta(seconds=400)


def test_check_brakes_triggers_no_progress_limit_when_pid_is_dead(orchestration, db_session):
    from datetime import datetime, timedelta, timezone

    task = _task(db_session, "BRAKE-PID-2", mode="bypass")
    old_time = datetime.now(timezone.utc) - timedelta(seconds=500)
    run = AgentRun(
        id="run-pid-dead",
        task_id=task.id,
        agent_id="@executor",
        cli="claude",
        command="claude -p 'test'",
        status="running",
        started_at=old_time,
        updated_at=old_time,
        pid=99999999,
    )
    db_session.add(run)
    db_session.commit()

    brake = orchestration.check_brakes(task, run_id=run.id, audit=True)

    assert brake.allowed is False
    assert brake.code == "no_progress_limit"
