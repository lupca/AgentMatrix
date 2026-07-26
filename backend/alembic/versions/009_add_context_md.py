"""Add context_md to projects table.

Revision ID: 009_add_context_md
Revises: 008_coordinator_session_model
Create Date: 2026-07-26
"""

from alembic import op
import sqlalchemy as sa

revision = "009_add_context_md"
down_revision = "008_coordinator_session_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("context_md", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "context_md")
