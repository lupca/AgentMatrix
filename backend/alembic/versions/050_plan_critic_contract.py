"""Extend the plan contract and persist independent critic outcomes."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "050_plan_critic_contract"
down_revision: str | None = "049_spec_task_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    jsonb = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("tasks", sa.Column("constraints", jsonb, nullable=False, server_default="[]"))
    op.add_column("tasks", sa.Column("evidence", jsonb, nullable=False, server_default="[]"))
    op.add_column("tasks", sa.Column("prior_art", jsonb, nullable=False, server_default="[]"))
    op.add_column("tasks", sa.Column("ruled_out", jsonb, nullable=False, server_default="[]"))
    op.add_column("tasks", sa.Column("limits", jsonb, nullable=True))
    op.add_column("tasks", sa.Column("planner", sa.String(50), nullable=True))
    op.add_column("tasks", sa.Column("plan_critic", sa.String(50), nullable=True))
    op.add_column("tasks", sa.Column("plan_critic_status", sa.String(10), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("plan_critic_findings", jsonb, nullable=False, server_default="[]"),
    )
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            "ck_tasks_plan_four_eyes",
            "planner IS NULL OR plan_critic IS NULL "
            "OR lower(trim(planner)) <> lower(trim(plan_critic))",
        )
        batch_op.create_check_constraint(
            "ck_tasks_plan_critic_status",
            "plan_critic_status IS NULL OR plan_critic_status IN ('accept', 'reject')",
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_plan_critic_status", type_="check")
        batch_op.drop_constraint("ck_tasks_plan_four_eyes", type_="check")
    for column in (
        "plan_critic_findings",
        "plan_critic_status",
        "plan_critic",
        "planner",
        "limits",
        "ruled_out",
        "prior_art",
        "evidence",
        "constraints",
    ):
        op.drop_column("tasks", column)
