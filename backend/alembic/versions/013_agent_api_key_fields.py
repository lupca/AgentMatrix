"""Add API-agent configuration and encrypted API-key storage."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "013_agent_api_key_fields"
down_revision: Union[str, None] = "012_session_listing_index_pinned"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("agent_type", sa.String(length=10), nullable=True, server_default="cli"),
    )
    op.add_column("agents", sa.Column("api_key", sa.String(length=500), nullable=True))
    op.add_column("agents", sa.Column("provider", sa.String(length=50), nullable=True))
    op.execute(sa.text("UPDATE agents SET agent_type = 'cli' WHERE agent_type IS NULL"))


def downgrade() -> None:
    op.drop_column("agents", "provider")
    op.drop_column("agents", "api_key")
    op.drop_column("agents", "agent_type")
