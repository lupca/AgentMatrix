"""Add tool_metrics — persistent telemetry for token-saving tool usage.

Revision ID: 035_add_tool_metrics
Revises: 034_add_task_landed_ref
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "035_add_tool_metrics"
down_revision: Union[str, None] = "034_add_task_landed_ref"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_metrics",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tool", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("task_id", sa.String(length=50), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=True),
        sa.Column("bytes_out", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_tool_metrics_tool", "tool_metrics", ["tool"])
    op.create_index("ix_tool_metrics_created_at", "tool_metrics", ["created_at"])


def downgrade() -> None:
    op.drop_table("tool_metrics")
