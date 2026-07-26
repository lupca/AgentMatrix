"""Add durable agent execution and output history tables.

Revision ID: 004_agent_runs
Revises: 003_schema_alignment
Create Date: 2026-07-26 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_agent_runs"
down_revision: Union[str, None] = "003_schema_alignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=20), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("cli", sa.String(length=20), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("dramatiq_message_id", sa.String(length=50), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timeout_seconds", sa.Integer(), server_default="14400", nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("result_ref", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("output_lines", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_agent_runs_timeout_positive"),
        sa.CheckConstraint("attempt > 0", name="ck_agent_runs_attempt_positive"),
        sa.CheckConstraint("max_attempts > 0", name="ck_agent_runs_max_attempts_positive"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_agent_runs_task", "agent_runs", ["task_id"])
    op.create_index("idx_agent_runs_status", "agent_runs", ["status"])

    op.create_table(
        "agent_output_chunks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_output_chunks_index_nonnegative"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "chunk_index", name="uq_output_chunks_run_index"),
    )
    op.create_index(
        "idx_output_chunks_run",
        "agent_output_chunks",
        ["run_id", "chunk_index"],
    )


def downgrade() -> None:
    op.drop_index("idx_output_chunks_run", table_name="agent_output_chunks")
    op.drop_table("agent_output_chunks")
    op.drop_index("idx_agent_runs_status", table_name="agent_runs")
    op.drop_index("idx_agent_runs_task", table_name="agent_runs")
    op.drop_table("agent_runs")
