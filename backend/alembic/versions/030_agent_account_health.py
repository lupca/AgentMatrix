"""Add CLI account health and per-run resource usage (CTV2-203)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "030_agent_account_health"
down_revision: Union[str, None] = "029_task_terminal_approval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("cli", sa.String(length=20), nullable=False),
        sa.Column("subscription_plan", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="healthy"),
        sa.Column("quota_pressure", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rate_limit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health_score", sa.Float(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id", "cli", name="uq_agent_accounts_agent_cli"),
        sa.CheckConstraint("quota_pressure >= 0 AND quota_pressure <= 1", name="ck_agent_accounts_quota_pressure"),
        sa.CheckConstraint("health_score >= 0 AND health_score <= 1", name="ck_agent_accounts_health_score"),
    )
    op.create_index("ix_agent_accounts_agent_id", "agent_accounts", ["agent_id"])
    op.create_table(
        "run_resource_usage",
        sa.Column("agent_run_id", sa.String(length=36), primary_key=True),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bash_commands", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_read", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("rate_limit_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 8), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("llm_calls >= 0", name="ck_run_resource_usage_llm_calls"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0", name="ck_run_resource_usage_tokens"),
        sa.CheckConstraint("estimated_cost_usd >= 0", name="ck_run_resource_usage_cost"),
    )


def downgrade() -> None:
    op.drop_table("run_resource_usage")
    op.drop_index("ix_agent_accounts_agent_id", table_name="agent_accounts")
    op.drop_table("agent_accounts")
