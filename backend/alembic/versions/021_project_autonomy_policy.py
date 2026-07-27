"""Add per-project autonomy policy overrides (CTV2-093).

Revision ID: 021_project_autonomy_policy
Revises: 020_task_dependencies
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "021_project_autonomy_policy"
down_revision: Union[str, None] = "020_task_dependencies"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("autonomy_policy", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("autonomy_policy")
