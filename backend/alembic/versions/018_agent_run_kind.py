"""Add agent_runs.kind/agent_role so review becomes an agent run kind (CTV2-086).

Revision ID: 018_agent_run_kind
Revises: 017_project_task_seq
Create Date: 2026-07-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "018_agent_run_kind"
down_revision: Union[str, None] = "017_project_task_seq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind", sa.String(length=20), nullable=False, server_default="execute"
            )
        )
        batch_op.add_column(
            sa.Column(
                "agent_role",
                sa.String(length=20),
                nullable=False,
                server_default="executor",
            )
        )

    op.execute("UPDATE agent_runs SET kind = 'execute', agent_role = 'executor'")

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.create_check_constraint(
            "ck_agent_runs_kind", "kind IN ('execute', 'review')"
        )
        batch_op.create_check_constraint(
            "ck_agent_runs_agent_role",
            "agent_role IN ('executor', 'reviewer')",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("ck_agent_runs_agent_role", type_="check")
        batch_op.drop_constraint("ck_agent_runs_kind", type_="check")
        batch_op.drop_column("agent_role")
        batch_op.drop_column("kind")
