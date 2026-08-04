"""Add structured AgentRun termination attribution and legacy marker."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "051_agent_run_failure_classification"
down_revision: str | None = "050_plan_critic_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("failure_category", sa.String(30), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("failure_data_quality", sa.String(20), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE agent_runs SET failure_category = 'unknown' "
            "WHERE failure_category IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE agent_runs SET failure_data_quality = CASE "
            "WHEN created_at < '2026-08-04 00:00:00+00' THEN 'legacy' "
            "ELSE 'current' END WHERE failure_data_quality IS NULL"
        )
    )
    # batch_alter_table keeps the migration portable to the SQLite migration
    # test database, whose dialect has no native ALTER COLUMN support.
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.alter_column(
            "failure_category", nullable=False, server_default="unknown"
        )
        batch_op.alter_column(
            "failure_data_quality", nullable=False, server_default="current"
        )
        batch_op.create_check_constraint(
            "ck_agent_runs_failure_category",
            "failure_category IN ("
            "'infra_timeout', 'infra_config', 'infra_conflict', 'infra_parse', "
            "'agent_no_output', 'agent_wrong', 'agent_incomplete', "
            "'brake_stopped', 'cancelled', 'unknown')",
        )
        batch_op.create_check_constraint(
            "ck_agent_runs_failure_data_quality",
            "failure_data_quality IN ('current', 'legacy')",
        )
    op.create_index(
        "ix_agent_runs_failure_category", "agent_runs", ["failure_category"]
    )
    op.create_index(
        "ix_agent_runs_failure_data_quality", "agent_runs", ["failure_data_quality"]
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint(
            "ck_agent_runs_failure_data_quality", type_="check"
        )
        batch_op.drop_constraint("ck_agent_runs_failure_category", type_="check")
    op.drop_index("ix_agent_runs_failure_data_quality", table_name="agent_runs")
    op.drop_index("ix_agent_runs_failure_category", table_name="agent_runs")
    op.drop_column("agent_runs", "failure_data_quality")
    op.drop_column("agent_runs", "failure_category")
