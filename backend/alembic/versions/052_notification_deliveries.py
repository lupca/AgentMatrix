"""Add notification_deliveries table (CTV2-1381)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "052_notification_deliveries"
down_revision: str | None = "051_agent_run_failure_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.String(20), nullable=False),
        sa.Column("task_event_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, server_default="telegram"),
        sa.Column("chat_id", sa.String(50), nullable=True),
        sa.Column("correlation_token", sa.String(36), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_event_id"], ["task_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_event_id"),
        sa.UniqueConstraint("correlation_token"),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_notification_deliveries_status",
        ),
    )
    op.create_index(
        "ix_notification_deliveries_task_id",
        "notification_deliveries",
        ["task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_task_id", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
