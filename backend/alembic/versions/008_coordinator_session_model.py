"""Add SDK coordinator provider and model selection to sessions.

Revision ID: 008_coordinator_session_model
Revises: 007_gate_system
Create Date: 2026-07-26 22:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "008_coordinator_session_model"
down_revision: Union[str, None] = "007_gate_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("selected_provider", sa.String(length=30), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("selected_model", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "selected_model")
    op.drop_column("sessions", "selected_provider")
