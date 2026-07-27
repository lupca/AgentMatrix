"""Add agent_runs.effort so dispatch can override the agent's default effort (CTV2-113).

Revision ID: 023_agent_run_effort
Revises: 022_archivable_entities
Create Date: 2026-07-28 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "023_agent_run_effort"
down_revision: Union[str, None] = "022_archivable_entities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("effort", sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_column("effort")
