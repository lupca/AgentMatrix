"""Add task_events table for polling notification architecture (CTV2-114).

Revision ID: 024_task_events_table
Revises: 023_agent_run_effort
Create Date: 2026-07-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "024_task_events_table"
down_revision: Union[str, None] = "023_agent_run_effort"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.String(length=20),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_task_events_created_at", "task_events", ["created_at"])
    op.create_index("idx_task_events_task_id", "task_events", ["task_id"])
    op.create_index(
        "idx_task_events_type_created",
        "task_events",
        ["event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_task_events_type_created", table_name="task_events")
    op.drop_index("idx_task_events_task_id", table_name="task_events")
    op.drop_index("idx_task_events_created_at", table_name="task_events")
    op.drop_table("task_events")
