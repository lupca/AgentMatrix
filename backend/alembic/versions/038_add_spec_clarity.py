"""Add persisted spec clarity and open questions to tasks.

Revision ID: 038_add_spec_clarity
Revises: 037_relax_cr_awaiting_check
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "038_add_spec_clarity"
down_revision: Union[str, None] = "037_relax_cr_awaiting_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("open_questions", sa.JSON(), nullable=True))
    op.add_column("tasks", sa.Column("spec_clarity", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "spec_clarity")
    op.drop_column("tasks", "open_questions")
