"""Add dispatch_decisions + dispatch_candidates tables (CTV2-202).

Revision ID: 028_dispatch_decisions
Revises: 027_outbox_events
Create Date: 2026-07-29 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "028_dispatch_decisions"
down_revision: Union[str, None] = "027_outbox_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dispatch_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=20), nullable=False),
        sa.Column("task_round_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="execute"),
        sa.Column("policy_version", sa.String(length=50), nullable=False),
        sa.Column("task_feature_snapshot", sa.JSON(), nullable=True),
        sa.Column("selected_agent_id", sa.String(length=50), nullable=False),
        sa.Column("selected_score", sa.Float(), nullable=True),
        sa.Column("selection_reason", sa.Text(), nullable=True),
        sa.Column("exploration", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("human_override", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_round_id"], ["task_rounds.id"]),
        sa.CheckConstraint("kind IN ('execute', 'review')", name="ck_dispatch_decisions_kind"),
    )
    op.create_index("ix_dispatch_decisions_task_id", "dispatch_decisions", ["task_id"])
    op.create_index(
        "ix_dispatch_decisions_task_round_id", "dispatch_decisions", ["task_round_id"]
    )

    op.create_table(
        "dispatch_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("dispatch_decision_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("predicted_pass1", sa.Float(), nullable=True),
        sa.Column("predicted_runtime", sa.Float(), nullable=True),
        sa.Column("quota_pressure", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dispatch_decision_id"], ["dispatch_decisions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "dispatch_decision_id", "agent_id", name="uq_dispatch_candidates_decision_agent"
        ),
    )
    op.create_index(
        "ix_dispatch_candidates_decision_id", "dispatch_candidates", ["dispatch_decision_id"]
    )

    # batch_alter_table so this also works on SQLite, which can't ALTER in a
    # foreign key after the fact -- it recreates the table under the hood.
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("dispatch_decision_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_agent_runs_dispatch_decision_id",
            "dispatch_decisions",
            ["dispatch_decision_id"],
            ["id"],
        )
    op.create_index(
        "ix_agent_runs_dispatch_decision_id", "agent_runs", ["dispatch_decision_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_runs_dispatch_decision_id", table_name="agent_runs")
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("fk_agent_runs_dispatch_decision_id", type_="foreignkey")
        batch_op.drop_column("dispatch_decision_id")

    op.drop_index("ix_dispatch_candidates_decision_id", table_name="dispatch_candidates")
    op.drop_table("dispatch_candidates")

    op.drop_index("ix_dispatch_decisions_task_round_id", table_name="dispatch_decisions")
    op.drop_index("ix_dispatch_decisions_task_id", table_name="dispatch_decisions")
    op.drop_table("dispatch_decisions")
