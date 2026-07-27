"""Add task_dependencies table so the driver can gate dispatch on a DAG of
task dependencies instead of relying on context-window ordering (CTV2-094).

Revision ID: 020_task_dependencies
Revises: 019_legacy_no_ac
Create Date: 2026-07-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "020_task_dependencies"
down_revision: Union[str, None] = "019_legacy_no_ac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String(length=20), nullable=False),
        sa.Column("depends_on_task_id", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["depends_on_task_id"], ["tasks.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("task_id", "depends_on_task_id"),
        sa.CheckConstraint(
            "task_id <> depends_on_task_id", name="ck_task_dependencies_no_self"
        ),
    )
    op.create_index(
        "ix_task_dependencies_depends_on",
        "task_dependencies",
        ["depends_on_task_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_dependencies_depends_on", table_name="task_dependencies")
    op.drop_table("task_dependencies")
