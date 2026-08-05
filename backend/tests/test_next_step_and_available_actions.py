"""CTV2-1394: the system must say the next move instead of relying on the
orchestrator to remember it.

On 2026-08-05 an orchestrator sat stuck on a `failed` task for 40 minutes: the
only error it got was "Task CTV2-1382 expected status 'dispatched', found
'failed'" -- true, but useless, because it named no tool to call next. The fix
found that day was `reopen_task`, and it was found only by chance, remembering
a code comment.

Two things pin the fix:

* every state-conflict error names the unblocking tool (`describe_next_step`);
* `get_status` reports `available_actions` -- which tools are valid from the
  current status and why, and which are blocked and how to unblock them
  (`TaskValidator.available_actions`).

The anti-drift test is the one that matters most: `available_actions` is
derived by calling the real FSM gates (`assert_status`,
`require_approved_pass_verdict`), not a hand-copied status table, so it cannot
silently drift the way every hand-written mirror of this system's behaviour
has drifted before (CTV2-1382, CTV2-1388, CTV2-1389). The test proves this by
actually calling, for every task status, every tool `available_actions` calls
valid -- and asserting none of those real calls raises a state-conflict.
"""

import pytest

from app.db.models import Agent, AgentRun, GateRecord, Project, Task
from app.services.command_router import CommandRouter
from app.services.task_orchestration import (
    TaskOrchestrationService,
    TransitionConflictError,
)
from app.services.task_validators import TaskValidator


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
        "title": "Next-step task",
        "mode": "bypass",
        "acceptance_criteria": ["AC 1"],
    }
    values.update(overrides)
    task = Task(**values)
    db.add(task)
    db.commit()
    return task


# ---------------------------------------------------------------------------
# (A) errors name the unblocking tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_conflict_on_failed_task_names_reopen_task(service, db_session):
    """A `failed` task with a delivered result: attach_result must name
    reopen_task as the way out, not just report the mismatch."""

    _add_task(
        db_session,
        "NEXT-001",
        status="failed",
        executor="@executor",
        result_ref="base..head",
        error="Task token limit reached",
    )

    router = CommandRouter(db_session)
    response = await router._handle_attach_result("NEXT-001 deadbeef", "test-session")

    assert "error" in response
    assert "reopen_task" in response["error"] or "reopen_task" in str(
        response.get("next_step", {})
    )
    assert response["next_step"]["state"] == "failed"
    assert "reopen_task" in response["next_step"]["next"]
    assert "result_ref" in response["next_step"]["why"]


@pytest.mark.asyncio
async def test_state_conflict_on_failed_task_without_result_ref_still_names_reopen(
    service, db_session
):
    _add_task(db_session, "NEXT-002", status="failed", executor="@executor")

    router = CommandRouter(db_session)
    response = await router._handle_attach_result("NEXT-002 deadbeef", "test-session")

    assert "error" in response
    assert "reopen_task" in response["next_step"]["next"]


def test_describe_next_step_shape(db_session):
    task = _add_task(
        db_session, "NEXT-003", status="failed", result_ref="base..head"
    )
    step = TaskValidator(db_session).describe_next_step(task)

    assert set(step) == {"state", "why", "next"}
    assert step["state"] == "failed"


# ---------------------------------------------------------------------------
# (B) get_status exposes available_actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_status_reports_available_and_blocked_actions_for_failed_task(
    service, db_session
):
    _add_task(
        db_session,
        "NEXT-004",
        status="failed",
        executor="@executor",
        result_ref="base..head",
    )

    router = CommandRouter(db_session)
    response = await router._handle_get_status("NEXT-004", "")

    actions = response["task"]["available_actions"]
    available_tools = {item["tool"] for item in actions["available"]}
    blocked_tools = {item["tool"]: item for item in actions["blocked"]}

    assert "reopen_task" in available_tools
    assert "land_task" in blocked_tools
    assert blocked_tools["land_task"]["fix"]


# ---------------------------------------------------------------------------
# Anti-drift: whatever available_actions calls valid, calling for real must
# not raise a state-conflict.
# ---------------------------------------------------------------------------


def _dispatch_call(service, task, key):
    return service.request_dispatch(
        task_id=task.id, agent_id="@executor", actor="chat:test", idempotency_key=key
    )


def _attach_result_call(service, task, key):
    return service.attach_result(
        task_id=task.id, commit="deadbeef", actor="chat:test", idempotency_key=key
    )


def _request_review_call(service, task, key):
    return service.request_review(
        task_id=task.id, reviewer="@reviewer", actor="chat:test", idempotency_key=key
    )


def _record_verdict_call(service, task, key):
    return service.request_verdict(
        task_id=task.id,
        verdict="changes",
        ac_results=[{"passed": False}],
        actor="chat:test",
        idempotency_key=key,
    )


def _reopen_task_call(service, task, key):
    return service.reopen_failed_task(task_id=task.id, actor="chat:test")


def _land_task_call(service, task, key):
    return service.land_task(task_id=task.id, actor="chat:test")


_TOOL_CALLS = {
    "dispatch_task": _dispatch_call,
    "attach_result": _attach_result_call,
    "request_review": _request_review_call,
    "record_verdict": _record_verdict_call,
    "reopen_task": _reopen_task_call,
    "land_task": _land_task_call,
}


def _task_for_status(db, task_id: str, status: str) -> Task:
    common = {"executor": "@executor"}
    if status == "todo":
        return _add_task(db, task_id, status="todo")
    if status == "dispatched":
        return _add_task(db, task_id, status="dispatched", **common)
    if status == "awaiting-review":
        return _add_task(
            db, task_id, status="awaiting-review", result_ref="base..head", **common
        )
    if status == "in-review":
        task = _add_task(
            db,
            task_id,
            status="in-review",
            result_ref="base..head",
            reviewer="@reviewer",
            **common,
        )
        db.add(
            AgentRun(
                id=f"{task_id}-review-run",
                task_id=task_id,
                agent_id="@reviewer",
                cli="codex",
                command="codex exec /code-review",
                kind="review",
                agent_role="reviewer",
                status="success",
            )
        )
        db.commit()
        return task
    if status == "changes-requested":
        return _add_task(db, task_id, status="changes-requested", **common)
    if status == "failed_delivered":
        return _add_task(
            db, task_id, status="failed", result_ref="base..head", **common
        )
    if status == "failed_undelivered":
        return _add_task(db, task_id, status="failed", **common)
    if status == "done":
        task = _add_task(
            db,
            task_id,
            status="done",
            result_ref="base..head",
            reviewer="@reviewer",
            verdict="pass",
            **common,
        )
        db.add(
            GateRecord(
                task_id=task_id,
                gate_type="verdict",
                status="approved",
                actor="@reviewer",
                mode="bypass",
                output_ref="pass",
                output_payload={"verdict": "pass"},
                idempotency_key=f"{task_id}-verdict",
                input_hash="hash",
            )
        )
        db.commit()
        return task
    if status == "cancelled":
        return _add_task(db, task_id, status="cancelled", **common)
    raise AssertionError(status)


@pytest.mark.parametrize(
    "status",
    [
        "todo",
        "dispatched",
        "awaiting-review",
        "in-review",
        "changes-requested",
        "failed_delivered",
        "failed_undelivered",
        "done",
        "cancelled",
    ],
)
def test_available_actions_never_promises_a_tool_that_state_conflicts(
    service, db_session, status
):
    task_id = f"DRIFT-{status.upper()}"
    task = _task_for_status(db_session, task_id, status)

    actions = TaskValidator(db_session).available_actions(task)
    available_tools = [item["tool"] for item in actions["available"]]

    if status != "cancelled":
        # 'cancelled' is terminal with no way forward through any of these
        # tools -- everything else must offer at least one live move.
        assert available_tools, f"available_actions found nothing valid for {status!r}"

    for tool in available_tools:
        call = _TOOL_CALLS[tool]
        try:
            call(service, task, f"{task_id}-{tool}")
        except TransitionConflictError as exc:
            pytest.fail(
                f"available_actions said {tool!r} was valid from status "
                f"{status!r}, but the real call raised a state-conflict: {exc}"
            )
        except Exception:
            # Any other rejection (brake, missing independent reviewer,
            # incomplete review evaluations, ...) is fine -- the only thing
            # available_actions promises is the absence of a state-conflict.
            pass
