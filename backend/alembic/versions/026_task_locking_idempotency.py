"""Add task-level locking and idempotency constraints (CTV2-204).

Revision ID: 026_task_locking_idempotency
Revises: 025_task_rounds
Create Date: 2026-07-29 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "026_task_locking_idempotency"
down_revision: Union[str, None] = "025_task_rounds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(
            sa.Column("task_round_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(length=100), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_agent_runs_task_round_id", "task_rounds", ["task_round_id"], ["id"]
        )
        batch_op.create_unique_constraint(
            "uq_agent_runs_round_kind_attempt",
            ["task_round_id", "kind", "attempt"],
        )
        batch_op.create_unique_constraint(
            "uq_agent_runs_task_idempotency",
            ["task_id", "idempotency_key"],
        )
    op.create_index(
        "ix_agent_runs_task_round_id", "agent_runs", ["task_round_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_task_round_id", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("uq_agent_runs_task_idempotency", type_="unique")
        batch_op.drop_constraint("uq_agent_runs_round_kind_attempt", type_="unique")
        batch_op.drop_constraint("fk_agent_runs_task_round_id", type_="foreignkey")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("task_round_id")
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("version")
