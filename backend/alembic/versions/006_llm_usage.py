"""Add the LLM token and cost telemetry ledger.

Revision ID: 006_llm_usage
Revises: 005_task_tags
Create Date: 2026-07-26 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "006_llm_usage"
down_revision: Union[str, None] = "005_task_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("task_id", sa.String(length=20), nullable=True),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("operation", sa.String(length=30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=8), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("input_tokens >= 0", name="ck_llm_usage_input_nonnegative"),
        sa.CheckConstraint("output_tokens >= 0", name="ck_llm_usage_output_nonnegative"),
        sa.CheckConstraint("cached_tokens >= 0", name="ck_llm_usage_cached_nonnegative"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_llm_usage_cost_nonnegative"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_llm_usage_latency_nonnegative"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_llm_usage_session", "llm_usage", ["session_id"])
    op.create_index("idx_llm_usage_task", "llm_usage", ["task_id"])
    op.create_index("idx_llm_usage_agent_run", "llm_usage", ["agent_run_id"])
    op.create_index("idx_llm_usage_model", "llm_usage", ["model"])
    op.create_index("idx_llm_usage_provider", "llm_usage", ["provider"])
    op.create_index("idx_llm_usage_operation", "llm_usage", ["operation"])


def downgrade() -> None:
    op.drop_index("idx_llm_usage_operation", table_name="llm_usage")
    op.drop_index("idx_llm_usage_provider", table_name="llm_usage")
    op.drop_index("idx_llm_usage_model", table_name="llm_usage")
    op.drop_index("idx_llm_usage_agent_run", table_name="llm_usage")
    op.drop_index("idx_llm_usage_task", table_name="llm_usage")
    op.drop_index("idx_llm_usage_session", table_name="llm_usage")
    op.drop_table("llm_usage")
