"""Add pinned to ix_sessions_context_listing (leading sort key after filters).

Revision ID: 012_session_listing_index_pinned
Revises: 011_session_hierarchical_context
Create Date: 2026-07-27
"""

from typing import Sequence, Union

from alembic import op


revision: str = "012_session_listing_index_pinned"
down_revision: Union[str, None] = "011_session_hierarchical_context"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_context_listing")
        batch_op.create_index(
            "ix_sessions_context_listing",
            ["context_level", "project_id", "status", "pinned", "last_activity_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_context_listing")
        batch_op.create_index(
            "ix_sessions_context_listing",
            ["context_level", "project_id", "status", "last_activity_at"],
        )
