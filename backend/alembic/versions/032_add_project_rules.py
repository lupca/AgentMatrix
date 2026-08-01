"""Add project_rules table and project context_generated field.

Revision ID: 032_add_project_rules
Revises: 031_agent_events
Create Date: 2026-07-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "032_add_project_rules"
down_revision: Union[str, None] = "031_agent_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "project_rules",
        sa.Column("id", sa.String(length=50), primary_key=True, nullable=False),
        sa.Column(
            "project_id",
            sa.String(length=50),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("globs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "name", name="uq_project_rules_project_name"),
    )
    op.create_index("ix_project_rules_project_id", "project_rules", ["project_id"])

    op.add_column(
        "projects",
        sa.Column(
            "context_generated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "context_generated")
    op.drop_index("ix_project_rules_project_id", table_name="project_rules")
    op.drop_table("project_rules")
