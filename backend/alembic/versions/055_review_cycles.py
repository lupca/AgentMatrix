"""Add review_cycles / review_findings, backfill from gate_records (CTV2-1379).

Verdict and finding data used to live only inside gate_records.input_payload
(JSON, unqueryable) and TaskRound.findings_ref (a frozen blob with no
per-finding lifecycle). This gives both a queryable, relational home.

Backfill is idempotent by CONSTRAINT, not by promise: source_gate_record_id
is a nullable FK to gate_records.id with a PARTIAL unique index (only rows
that came from the backfill carry a value), and the backfill INSERT uses
ON CONFLICT DO NOTHING -- running it twice inserts the same rows once. Rows
whose gate_records.input_payload has no ac_results are left alone; an empty
review_cycle row would be worse than no row (it would look like a real review
and get counted in every stat). agent_run_id is left NULL wherever it cannot
be resolved -- never guessed.

`alembic heads` showed exactly one head, 054_task_coordinator_notes,
immediately before this file was authored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "055_review_cycles"
down_revision: str | None = "054_task_coordinator_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_cycles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(20),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "task_round_id",
            sa.String(36),
            sa.ForeignKey("task_rounds.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("reviewer_id", sa.String(50), nullable=True),
        sa.Column(
            "reviewer_agent_run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="requested"),
        sa.Column("verdict", sa.String(10), nullable=True),
        sa.Column(
            "source_gate_record_id",
            sa.Integer,
            sa.ForeignKey("gate_records.id"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'submitted', 'pass', 'changes', 'abandoned')",
            name="ck_review_cycles_status",
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('pass', 'changes')",
            name="ck_review_cycles_verdict",
        ),
    )
    op.create_index(
        "ix_review_cycles_source_gate_record_id_unique",
        "review_cycles",
        ["source_gate_record_id"],
        unique=True,
        postgresql_where=sa.text("source_gate_record_id IS NOT NULL"),
        sqlite_where=sa.text("source_gate_record_id IS NOT NULL"),
    )

    op.create_table(
        "review_findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "review_cycle_id",
            sa.String(36),
            sa.ForeignKey("review_cycles.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("severity", sa.String(10), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("waived_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('open', 'fixed', 'waived')",
            name="ck_review_findings_status",
        ),
        sa.CheckConstraint(
            "status <> 'waived' OR (waived_reason IS NOT NULL AND trim(waived_reason) <> '')",
            name="ck_review_findings_waived_reason",
        ),
    )

    _backfill(op.get_bind())


def _backfill(bind) -> None:
    """Insert one review_cycles row per historical verdict gate_record.

    Filter: gate_type='verdict' AND input_payload has an 'ac_results' key.
    task_round_id is resolved from task_rounds by matching reviewer_run_id
    to the agent_run that produced the review (best-effort join through
    task_rounds.reviewer_run_id / task_id + status), falling back to the
    task's rounds table when a direct run match isn't available. Rows that
    cannot be tied to a task_round are skipped entirely (the FK is NOT NULL)
    -- skipped, not guessed.
    """
    dialect = bind.dialect.name
    json_has_key = (
        "input_payload::jsonb ? 'ac_results'"
        if dialect == "postgresql"
        else "json_extract(input_payload, '$.ac_results') IS NOT NULL"
    )
    rows = bind.execute(
        sa.text(
            f"""
            SELECT gr.id AS gate_record_id, gr.task_id, gr.reviewer,
                   gr.output_ref AS verdict, gr.created_at
            FROM gate_records gr
            WHERE gr.gate_type = 'verdict' AND {json_has_key}
            """
        )
    ).fetchall()

    for row in rows:
        task_round = bind.execute(
            sa.text(
                """
                SELECT id, reviewer_run_id
                FROM task_rounds
                WHERE task_id = :task_id
                ORDER BY round_no DESC
                LIMIT 1
                """
            ),
            {"task_id": row.task_id},
        ).fetchone()
        if task_round is None:
            continue
        verdict = row.verdict if row.verdict in ("pass", "changes") else None
        bind.execute(
            sa.text(
                """
                INSERT INTO review_cycles (
                    id, task_id, task_round_id, reviewer_id,
                    reviewer_agent_run_id, status, verdict,
                    source_gate_record_id, requested_at, submitted_at,
                    completed_at, created_at, updated_at
                ) VALUES (
                    :id, :task_id, :task_round_id, :reviewer_id,
                    :reviewer_agent_run_id, :status, :verdict,
                    :source_gate_record_id, :requested_at, :submitted_at,
                    :completed_at, :created_at, :updated_at
                )
                ON CONFLICT (source_gate_record_id)
                WHERE source_gate_record_id IS NOT NULL DO NOTHING
                """
            ),
            {
                "id": _uuid(),
                "task_id": row.task_id,
                "task_round_id": task_round.id,
                "reviewer_id": row.reviewer,
                "reviewer_agent_run_id": task_round.reviewer_run_id,
                "status": verdict or "abandoned",
                "verdict": verdict,
                "source_gate_record_id": row.gate_record_id,
                "requested_at": row.created_at,
                "submitted_at": row.created_at,
                "completed_at": row.created_at,
                "created_at": row.created_at,
                "updated_at": row.created_at,
            },
        )


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def downgrade() -> None:
    op.drop_table("review_findings")
    op.drop_index("ix_review_cycles_source_gate_record_id_unique", table_name="review_cycles")
    op.drop_table("review_cycles")
