"""Gate briefs, unknowns, and verdict evidence (CTV2-1393).

A gate used to hand the decider only an idempotency key. These tests check
that the derived brief actually reflects live DB state (not hand-written
text), that `unknowns` fires exactly when a task has no spec_task_link, and
that approving a verdict gate without evidence is refused with a brief and a
checklist instead of a silent rejection.
"""

import pytest

from app.db.models import (
    Agent,
    AgentRun,
    DispatchCandidate,
    DispatchDecision,
    GateRecord,
    LLMUsage,
    Project,
    SpecItem,
    SpecTaskLink,
    Task,
)
from app.mcp_native import _verdict_evidence_block, _GATE_CHECKS
from app.services.task_orchestration import TaskOrchestrationService
from app.services.task_state_machine import (
    TaskStateMachine,
    build_gate_brief,
    gate_unknowns,
    verdict_ac_checks,
)


@pytest.fixture
def service(db_session):
    db_session.add(Project(id="project", name="Project", repo_root="/tmp"))
    db_session.add(Agent(id="@executor", name="Executor", role="executor", cli="codex"))
    db_session.add(Agent(id="@reviewer", name="Reviewer", role="reviewer", cli="codex"))
    db_session.add(Agent(id="@other", name="Other", role="executor", cli="codex"))
    db_session.commit()
    return TaskOrchestrationService(db_session)


def _add_task(db, task_id: str, **overrides) -> Task:
    values = {
        "id": task_id,
        "project": "project",
        "title": "Gate brief task",
        "mode": "supervised",
        "acceptance_criteria": ["AC 1", "AC 2"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


def _add_terminal_review_run(db, task: Task, agent_id: str = "@reviewer") -> AgentRun:
    run = AgentRun(
        id=f"{task.id}-review-run",
        task_id=task.id,
        agent_id=agent_id,
        cli="codex",
        command="codex exec /code-review",
        kind="review",
        agent_role="reviewer",
        status="success",
    )
    db.add(run)
    db.commit()
    return run


# --- unknowns ---------------------------------------------------------------


def test_unknowns_present_when_no_spec_link(db_session, service):
    task = _add_task(db_session, "UNK-001", status="todo")
    assert gate_unknowns(db_session, task) == [
        "task này không gắn spec_item/impl_design — không có căn cứ để nói kết "
        "quả có đúng ý định hay không"
    ]


def test_unknowns_empty_when_spec_link_exists(db_session, service):
    task = _add_task(db_session, "UNK-002", status="todo")
    db_session.add(
        SpecItem(
            id="SPEC-001",
            project_id="project",
            kind="proposition",
            title="Spec item",
            body="Body text",
        )
    )
    db_session.commit()
    db_session.add(
        SpecTaskLink(
            spec_item_id="SPEC-001",
            task_id=task.id,
            relation="implements",
            created_by="test",
        )
    )
    db_session.commit()
    assert gate_unknowns(db_session, task) == []


# --- dispatch brief -----------------------------------------------------


def test_dispatch_brief_matches_scored_candidates_and_spend(db_session, service):
    task = _add_task(
        db_session,
        "BRIEF-DISP",
        status="todo",
        plan="Do the thing carefully.",
        risk="high",
    )
    db_session.add(
        LLMUsage(
            task_id=task.id,
            model="claude",
            provider="anthropic",
            operation="plan",
            input_tokens=1000,
            output_tokens=500,
            cost_usd="1.2345",
        )
    )
    db_session.commit()

    decision = DispatchDecision(
        id="dd-1",
        task_id=task.id,
        kind="execute",
        policy_version="v1",
        selected_agent_id="@executor",
        selected_score=0.9,
        selection_reason="best fit",
    )
    db_session.add(decision)
    db_session.add_all(
        [
            DispatchCandidate(
                dispatch_decision_id="dd-1",
                agent_id="@executor",
                eligible=True,
                final_score=0.9,
            ),
            DispatchCandidate(
                dispatch_decision_id="dd-1",
                agent_id="@other",
                eligible=False,
                rejection_reason="over quota",
                final_score=None,
            ),
        ]
    )
    db_session.commit()

    # GateRecord is append-only (DB-enforced) so it cannot be created via
    # request_dispatch and then edited to add dispatch_decision_id; build a
    # transient, never-persisted record with the same shape a real dispatch
    # gate carries. build_gate_brief only reads it, never writes it.
    record = GateRecord(
        task_id=task.id,
        gate_type="dispatch",
        status="pending",
        input_payload={
            "agent_id": "@executor",
            "kind": "execute",
            "dispatch_decision_id": "dd-1",
        },
    )

    brief = build_gate_brief(db_session, record)
    assert "@executor" in brief["summary"]
    assert "score=0.9" in brief["summary"]
    assert "@other" in brief["summary"]
    assert "over quota" in brief["summary"]
    assert "Do the thing carefully" in brief["summary"]
    assert "Số AC: 2" in brief["summary"]
    assert "$1.2345" in brief["summary"]
    assert "500" in brief["summary"] or "1500" in brief["summary"]
    assert "high" in brief["summary"]
    assert brief["unknowns"]  # no spec link on this task


# --- review_order brief ---------------------------------------------------


def test_review_order_brief_reports_four_eyes(db_session, service):
    task = _add_task(
        db_session,
        "BRIEF-REV",
        status="awaiting-review",
        executor="@executor",
        result_ref="base..head",
    )
    pending = service.request_review(
        task_id=task.id,
        reviewer="@reviewer",
        actor="@operator",
        idempotency_key="review-brief",
        selection_reason="independent and available",
    )
    brief = build_gate_brief(db_session, pending.gate_record)
    assert "@reviewer" in brief["summary"]
    assert "independent and available" in brief["summary"]
    assert "OK" in brief["summary"]


# --- verdict brief + evidence ----------------------------------------------


def _to_verdict_pending(db_session, service, task_id: str):
    task = _add_task(
        db_session,
        task_id,
        status="in-review",
        executor="@executor",
        reviewer="@reviewer",
        result_ref="base..head",
    )
    _add_terminal_review_run(db_session, task)
    pending = service.request_verdict(
        task_id=task.id,
        verdict="pass",
        ac_results=[
            {"id": "AC 1", "passed": True, "evidence": "pytest -k ac1 green"},
            {"id": "AC 2", "passed": True, "evidence": "pytest -k ac2 green"},
        ],
        findings=[{"severity": "minor", "text": "nit"}],
        actor="@reviewer",
        idempotency_key=f"verdict-{task_id}",
    )
    return task, pending


def test_verdict_brief_lists_each_ac_and_findings(db_session, service):
    task, pending = _to_verdict_pending(db_session, service, "BRIEF-VER")
    brief = build_gate_brief(db_session, pending.gate_record)
    assert "pass" in brief["summary"]
    assert "pytest -k ac1 green" in brief["summary"]
    assert "pytest -k ac2 green" in brief["summary"]
    assert "minor=1" in brief["summary"]


def test_verdict_evidence_required_returns_brief_and_checks(db_session, service):
    task, pending = _to_verdict_pending(db_session, service, "EVID-001")

    blocked = _verdict_evidence_block(
        db_session, {"gate_record_id": str(pending.gate_record.id), "decision": "approved"}
    )
    assert blocked is not None
    assert blocked["ok"] is False
    assert blocked["error"]["code"] == "evidence_required"
    assert "brief" in blocked
    assert blocked["checks"]
    assert any("AC 1" in c for c in blocked["checks"])


def test_verdict_evidence_supplied_allows_call_through(db_session, service):
    task, pending = _to_verdict_pending(db_session, service, "EVID-002")

    allowed = _verdict_evidence_block(
        db_session,
        {
            "gate_record_id": str(pending.gate_record.id),
            "decision": "approved",
            "evidence": [{"check": "pytest -k ac1", "result": "1 passed"}],
        },
    )
    assert allowed is None


def test_evidence_is_persisted_on_the_decision_row_and_readable_after_approval(
    db_session, service
):
    task, pending = _to_verdict_pending(db_session, service, "EVID-003")

    result = service.decide_gate(
        gate_record_id=pending.gate_record.id,
        decision="approved",
        actor="@supervisor",
        idempotency_key="approve-with-evidence",
    )
    evidence = [
        {"check": "pytest -k ac1", "result": "1 passed"},
        {"check": "pytest -k ac2", "result": "1 passed"},
    ]
    evidence_record = TaskStateMachine(db_session).record_gate_evidence(
        result.gate_record.id, evidence
    )

    # Read it back the way a later caller would: via the parent_id link to
    # the decision row, not by re-reading (mutating) the decision itself.
    reread = (
        db_session.query(GateRecord)
        .filter(GateRecord.parent_id == result.gate_record.id, GateRecord.gate_type == "verdict_evidence")
        .one()
    )
    assert reread.id == evidence_record.id
    assert reread.output_payload["evidence"] == evidence

    # The decision row itself (status, parent chain) is untouched.
    decision_row = db_session.query(GateRecord).filter(GateRecord.id == result.gate_record.id).one()
    assert decision_row.status == "approved"
    assert decision_row.parent_id == pending.gate_record.id


def test_verdict_ac_checks_falls_back_to_generic_gate_check_when_no_ac_results(db_session):
    record = GateRecord(
        task_id="NOPE",
        gate_type="verdict",
        status="pending",
        input_payload={},
    )
    assert verdict_ac_checks(record) == []
    # mcp_native falls back to the shared _GATE_CHECKS text in this case.
    assert "verdict" in _GATE_CHECKS
