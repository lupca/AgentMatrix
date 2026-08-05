"""Derive "is this task waiting on a human?" from evidence, never from a flag.

`tasks.awaiting_approval` used to be a free-standing boolean that a dozen code
paths set and a dozen others cleared.  Measured on 2026-08-06 against the live
DB: 20 tasks carried the flag and 4 of them (CTLA-005, CTV2-1372, VOMA-003,
VOMA-015) had no unresolved gate at all.  Nothing was waiting on them and
nothing could clear them -- `approve_gate` answered "No pending gate found"
and every dispatch path refused a task with the flag set.  Permanently bricked
by a projection that had drifted off the truth it was supposed to project.

That is the fifth time this exact family of bug has landed in this system
(`task.status`, `pending_approvals_note`, `token_limit`, `next`, now this one).
The fix is the same every time: stop storing the conclusion, store only the
evidence, and compute the conclusion on demand.

This module is that computation.  `derive_approval_hold` is the single place
that answers the question, and `TaskStateMachine.sync_awaiting_approval` is
the single place allowed to write the answer back to the column.

See spec item 3e2a7102-f3a9-4a2f-99f1-28e735d98396 (CTV2-1401).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import exists
from sqlalchemy.orm import Session, aliased

from app.db.models import GateRecord, Task, TaskEvent

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

__all__ = [
    "ApprovalHold",
    "HUMAN_QUESTION_PREFIX",
    "LANDING_FAILED_PREFIX",
    "derive_approval_hold",
    "task_is_waiting_on_human",
]

#: `ask_human` marks its prompt so the answer path can tell its own block
#: apart from a real gate.  Kept here because both the writer and the deriver
#: need the exact same string.
HUMAN_QUESTION_PREFIX = "[human_question]"

#: `land_verdict_result` failures write this prefix into `task.error`.
LANDING_FAILED_PREFIX = "landing_failed:"

#: A task in one of these statuses can never hold: the DB constraint
#: `ck_tasks_terminal_not_awaiting_approval` forbids `done` + awaiting, and a
#: cancelled task has nobody left to answer.
TERMINAL_STATUSES = frozenset({"done", "cancelled"})


@dataclass(frozen=True)
class ApprovalHold:
    """One reason a task is waiting on a human, with the evidence that says so.

    ``source`` names which evidence produced it, so a caller can tell a gate
    decision (resolve with `approve_gate`) apart from a chat question (resolve
    with `ask_human {answer}`) apart from a stale plan (resolve by replanning).
    """

    source: str
    prompt: str
    gate_record_id: str | None = None


def _gate_hold(db: Session, task: Task) -> ApprovalHold | None:
    """An unresolved pending gate root.

    GateRecord is append-only: a decision is a *child* row, and the pending
    root keeps saying `pending` forever.  So "unresolved" means "pending with
    no decision child", never "status != pending".
    """
    decision = aliased(GateRecord)
    record = (
        db.query(GateRecord)
        .filter(
            GateRecord.task_id == task.id,
            GateRecord.status == "pending",
            ~exists().where(decision.parent_id == GateRecord.id),
        )
        .order_by(GateRecord.created_at.desc())
        .first()
    )
    if record is None:
        return None
    payload = record.input_payload if isinstance(record.input_payload, dict) else {}
    prompt = (
        str(payload.get("approval_prompt") or "").strip()
        or str(record.error_message or "").strip()
        or f"Approve {record.gate_type} gate for task {task.id} "
        f"(request {record.idempotency_key})?"
    )
    return ApprovalHold(source="gate", prompt=prompt, gate_record_id=record.id)


def _human_question_hold(db: Session, task: Task) -> ApprovalHold | None:
    """An `ask_human` question the human has not answered in chat yet.

    This wait is real but it is *not* a gate: the answer comes back through
    the chat session, through no tool at all, so there is no gate record to
    read.  Its durable evidence is the event pair -- a `human_question` event
    with no `human_answer` event after it.  Derivation that only looked at
    gates would silently unblock a task a human is still thinking about.
    """
    last_question = (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task.id, TaskEvent.event_type == "human_question")
        .order_by(TaskEvent.id.desc())
        .first()
    )
    if last_question is None:
        return None
    answered = (
        db.query(TaskEvent.id)
        .filter(
            TaskEvent.task_id == task.id,
            TaskEvent.event_type == "human_answer",
            TaskEvent.id > last_question.id,
        )
        .first()
    )
    if answered is not None:
        return None
    payload = last_question.payload if isinstance(last_question.payload, dict) else {}
    question = str(payload.get("question") or "").strip() or "(câu hỏi không còn nội dung)"
    return ApprovalHold(
        source="human_question",
        prompt=f"{HUMAN_QUESTION_PREFIX} {question}",
    )


def _spec_clarity_hold(task: Task) -> ApprovalHold | None:
    """The Spec Clarity Loop: the planner said it does not know enough yet."""
    open_questions = [
        str(q).strip() for q in (task.open_questions or []) if str(q).strip()
    ]
    clarity = task.spec_clarity
    # `None` means the task predates spec_clarity (legacy import) -- absence of
    # a measurement is not a low measurement, so it must not block.
    unclear = clarity is not None and clarity != "high"
    if not open_questions and not unclear:
        return None
    questions = "\n".join(
        f"{index}) {question}" for index, question in enumerate(open_questions, 1)
    )
    question_block = f"\n{questions}" if questions else ""
    return ApprovalHold(
        source="spec_clarity",
        prompt=(
            f"Spec chưa đủ rõ (clarity={clarity}). Cập nhật task.plan với "
            f"câu trả lời cho các câu hỏi sau (dùng update_task, trường plan) "
            f"rồi chạy lại generate_spec_plan — planner sẽ đọc task.plan ở vòng "
            f"tiếp theo:{question_block}"
        ),
    )


def _plan_critic_hold(task: Task) -> ApprovalHold | None:
    """The plan critic rejected this exact plan and nobody has replanned."""
    if task.plan_critic_status != "reject":
        return None
    findings = task.plan_critic_findings or []
    summary = ""
    if isinstance(findings, list) and findings:
        first = findings[0]
        if isinstance(first, dict):
            summary = str(first.get("title") or first.get("detail") or "").strip()
    detail = f": {summary}" if summary else ""
    return ApprovalHold(
        source="plan_critic",
        prompt=(
            f"Plan critic {task.plan_critic or '?'} rejected this plan{detail}. "
            "Correct the evidenced findings and run generate_spec_plan again."
        ),
    )


def _landing_hold(task: Task) -> ApprovalHold | None:
    """A pass verdict whose merge into main failed -- a human must fix the repo."""
    error = str(task.error or "")
    if not error.startswith(LANDING_FAILED_PREFIX):
        return None
    return ApprovalHold(
        source="landing",
        prompt=(
            f"Landing {task.result_ref} failed: "
            f"{error[len(LANDING_FAILED_PREFIX):].strip()} — "
            "fix the repo, then call land_task again."
        ),
    )


def derive_approval_hold(
    db: Session, task: Task, *, as_status: str | None = None
) -> ApprovalHold | None:
    """Return why this task waits on a human, or ``None`` if it does not.

    Ordered most-specific-first: a real gate outranks everything, because it
    is the one hold that has an id and a decision tool pointed at it.  The
    safety brake is in there too, as a `pending` ledger row rather than a
    side-flag (see `TaskValidator._record_brake`).

    ``as_status`` asks "what would the hold be if the task were at this
    status".  `transition_to_done` needs it: the DB constraint
    `ck_tasks_terminal_not_awaiting_approval` is checked on the same statement
    that writes `status='done'`, so the projection has to be written *before*
    the status flip, from the status the task is about to have.
    """
    if (as_status or task.status) in TERMINAL_STATUSES:
        return None
    return (
        _gate_hold(db, task)
        or _human_question_hold(db, task)
        or _spec_clarity_hold(task)
        or _plan_critic_hold(task)
        or _landing_hold(task)
    )


def task_is_waiting_on_human(db: Session, task: Task) -> bool:
    """Blocking-decision helper: never read the cached column for this.

    Every code path that *refuses to act* because a human is being waited on
    calls this, not `task.awaiting_approval`.  That is what makes a drifted
    column harmless: it can misreport in a listing for one beat, but it can no
    longer brick a task.
    """
    return derive_approval_hold(db, task) is not None
