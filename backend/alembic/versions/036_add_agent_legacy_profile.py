"""Add agents.legacy_profile — V1 markdown profile stats the importer dropped.

Revision ID: 036_add_agent_legacy_profile
Revises: 035_add_tool_metrics
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "036_add_agent_legacy_profile"
down_revision: Union[str, None] = "035_add_tool_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("legacy_profile", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "legacy_profile")
