"""Add task_rounds table for multi-round dispatch/review history (CTV2-201).

Revision ID: 025_task_rounds
Revises: 024_task_events_table
Create Date: 2026-07-29 00:00:00.000000
"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "025_task_rounds"
down_revision: Union[str, None] = "024_task_events_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_rounds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=20), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="dispatched"),
        sa.Column("base_sha", sa.String(length=100), nullable=True),
        sa.Column("plan_ref", sa.String(length=255), nullable=True),
        sa.Column("executor_agent_id", sa.String(length=50), nullable=True),
        sa.Column("executor_run_id", sa.String(length=36), nullable=True),
        sa.Column("reviewer_agent_id", sa.String(length=50), nullable=True),
        sa.Column("reviewer_run_id", sa.String(length=36), nullable=True),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("verdict", sa.String(length=10), nullable=True),
        sa.Column("findings_ref", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["executor_run_id"], ["agent_runs.id"]),
        sa.ForeignKeyConstraint(["reviewer_run_id"], ["agent_runs.id"]),
        sa.UniqueConstraint("task_id", "round_no", name="uq_task_rounds_task_round_no"),
    )
    op.create_index("ix_task_rounds_task_id", "task_rounds", ["task_id"])

    # batch_alter_table so this also works on SQLite, which can't ALTER in a
    # foreign key after the fact -- it recreates the table under the hood.
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("current_round_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("final_result_ref", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("final_verdict", sa.String(length=10), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_current_round_id",
            "task_rounds",
            ["current_round_id"],
            ["id"],
        )

    _backfill_rounds_for_existing_tasks()


def _backfill_rounds_for_existing_tasks() -> None:
    """One round_no=1 TaskRound per pre-existing dispatched task.

    A task migrated in place from before this table existed only has its
    flat executor/reviewer/result_ref/verdict columns -- there is no
    per-round history to recover, so everything collapses into a single
    "round 1" snapshot of whatever the task's current state is.
    """
    bind = op.get_bind()
    task_rounds = sa.table(
        "task_rounds",
        sa.column("id", sa.String()),
        sa.column("task_id", sa.String()),
        sa.column("round_no", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("executor_agent_id", sa.String()),
        sa.column("reviewer_agent_id", sa.String()),
        sa.column("result_ref", sa.String()),
        sa.column("verdict", sa.String()),
        sa.column("findings_ref", sa.JSON()),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    tasks = bind.execute(
        sa.text(
            """
            SELECT id, executor, reviewer, result_ref, verdict, findings,
                   status, dispatched_at, completed_at
            FROM tasks
            WHERE executor IS NOT NULL OR result_ref IS NOT NULL
            """
        )
    ).mappings().all()

    for task in tasks:
        round_id = str(uuid.uuid4())
        bind.execute(
            task_rounds.insert().values(
                id=round_id,
                task_id=task["id"],
                round_no=1,
                status=task["status"],
                executor_agent_id=task["executor"],
                reviewer_agent_id=task["reviewer"],
                result_ref=task["result_ref"],
                verdict=task["verdict"],
                findings_ref=task["findings"],
                started_at=task["dispatched_at"],
                completed_at=task["completed_at"],
            )
        )
        bind.execute(
            sa.text(
                "UPDATE tasks SET current_round_id = :round_id,"
                " final_result_ref = CASE WHEN verdict = 'pass' THEN result_ref ELSE final_result_ref END,"
                " final_verdict = CASE WHEN verdict = 'pass' THEN verdict ELSE final_verdict END"
                " WHERE id = :task_id"
            ),
            {"round_id": round_id, "task_id": task["id"]},
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("fk_tasks_current_round_id", type_="foreignkey")
        batch_op.drop_column("final_verdict")
        batch_op.drop_column("final_result_ref")
        batch_op.drop_column("current_round_id")
    op.drop_index("ix_task_rounds_task_id", table_name="task_rounds")
    op.drop_table("task_rounds")
