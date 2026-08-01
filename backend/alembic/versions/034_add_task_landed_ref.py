"""Add tasks.landed_ref — the merge commit that landed the result on main.

Revision ID: 034_add_task_landed_ref
Revises: 033_task_event_schema_v2
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "034_add_task_landed_ref"
down_revision: Union[str, None] = "033_task_event_schema_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("landed_ref", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "landed_ref")
