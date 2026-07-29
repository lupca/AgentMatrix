"""Add normalized agent events and retained vendor output (CTV2-209).

Revision ID: 031_agent_events
Revises: 030_agent_account_health
Create Date: 2026-07-29 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "031_agent_events"
down_revision: Union[str, None] = "030_agent_account_health"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('run.started', 'llm.requested', 'llm.completed', "
            "'tool.started', 'tool.completed', 'gate.requested', "
            "'workspace.changed', 'run.heartbeat', 'run.completed')",
            name="ck_agent_events_type",
        ),
        sa.CheckConstraint("seq >= 0", name="ck_agent_events_seq_nonnegative"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_events_run_seq"),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_index("ix_agent_events_event_type", "agent_events", ["event_type"])
    op.create_index("idx_agent_events_run_type", "agent_events", ["run_id", "event_type"])

    op.create_table(
        "vendor_raw_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("cli", sa.String(length=20), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("seq >= 0", name="ck_vendor_raw_events_seq_nonnegative"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "seq", name="uq_vendor_raw_events_run_seq"),
    )
    op.create_index("ix_vendor_raw_events_run_id", "vendor_raw_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_vendor_raw_events_run_id", table_name="vendor_raw_events")
    op.drop_table("vendor_raw_events")
    op.drop_index("idx_agent_events_run_type", table_name="agent_events")
    op.drop_index("ix_agent_events_event_type", table_name="agent_events")
    op.drop_index("ix_agent_events_run_id", table_name="agent_events")
    op.drop_table("agent_events")
