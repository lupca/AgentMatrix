"""The coordinator is told it decides -- derived from mode, not asserted.

Three texts shape every coordinator session: `docs/coordinator-rules.md` (copied
into CLAUDE.md/AGENTS.md/PROJECT.md by init-coordinator-workdir.sh),
`SERVER_INSTRUCTIONS` (injected into every MCP session), and
`pending_approvals_note` (attached to EVERY tool result).

Until CTV2-1391 all three said the opposite of the coordinator's actual
authority, and the third one -- read dozens of times per session -- was a
hardcoded string that never looked at the task's mode. The most repeated
sentence in the system was also the one contradicting it.
"""

import pathlib

from app.db.models import Project, Task
from app.mcp_native import SERVER_INSTRUCTIONS, _gate_decider, _pending_approvals_note

RULES = pathlib.Path(__file__).resolve().parents[2] / "docs" / "coordinator-rules.md"


def test_the_three_texts_no_longer_tell_the_coordinator_to_wait():
    rules = RULES.read_text()
    for forbidden in (
        "only after the human explicitly approves",
        "not permission to go around",
    ):
        assert forbidden not in rules, f"coordinator-rules.md still says: {forbidden}"

    for forbidden in (
        "explicit approval in chat",
        "not permission to bypass",
        "never restart processes yourself",
    ):
        assert forbidden not in SERVER_INSTRUCTIONS, (
            f"SERVER_INSTRUCTIONS still says: {forbidden}"
        )


def test_server_instructions_keep_the_limits_that_never_bend():
    lowered = SERVER_INSTRUCTIONS.lower()
    assert "four-eyes" in lowered
    assert "append-only" in lowered
    assert "verdict belongs to" in lowered


def test_server_instructions_stay_within_the_documented_budget():
    """The comment above it pins ~512 chars as Codex's effective window."""
    assert len(SERVER_INSTRUCTIONS) < 1600


class _Row:
    def __init__(self, task_id, gate_type):
        self.task_id = task_id
        self.gate_type = gate_type
        self.input_payload = {}


def _task(db, task_id, mode):
    db.add(Project(id="p-auth", name="P", repo_root="/tmp"))
    task = Task(id=task_id, project="p-auth", title="t", mode=mode)
    db.add(task)
    db.commit()
    return task


def test_note_is_derived_from_mode_not_hardcoded(db_session):
    _task(db_session, "AUTH-1", "bypass")

    decider, reason = _gate_decider(db_session, _Row("AUTH-1", "verdict"))
    assert decider == "coordinator"
    assert reason is None

    note = _pending_approvals_note(
        [{"id": "AUTH-1", "kind": "task:verdict", "decided_by": decider}]
    )
    assert "yours to decide" in note
    # And it says what to check for THIS gate type, not a generic "verify".
    assert "re-run the numbers" in note


def test_a_supervised_task_still_asks_the_human(db_session):
    _task(db_session, "AUTH-2", "supervised")

    decider, reason = _gate_decider(db_session, _Row("AUTH-2", "dispatch"))
    assert decider == "human"
    assert reason is None

    note = _pending_approvals_note(
        [{"id": "AUTH-2", "kind": "task:dispatch", "decided_by": decider}]
    )
    assert "human" in note


def test_an_unresolvable_mode_leans_toward_the_human_and_says_why(db_session):
    """Unable to tell who decides is not a licence to decide.

    The first cut fell back to "coordinator" whenever the lookup raised, which
    leans the wrong way: it would have said "this one is yours" on a task that
    actually wanted a human, and swallowed the underlying fault while doing it.
    """

    decider, reason = _gate_decider(db_session, _Row("NO-SUCH-TASK", "verdict"))
    assert decider == "unknown"
    assert reason and "not found" in reason

    note = _pending_approvals_note(
        [
            {
                "id": "NO-SUCH-TASK",
                "kind": "task:verdict",
                "decided_by": decider,
                "decider_unknown_reason": reason,
            }
        ]
    )
    assert "needing the human" in note
    assert "yours to decide" not in note
    # The reason travels with it -- a swallowed fault is the thing to avoid.
    assert "not found" in note
