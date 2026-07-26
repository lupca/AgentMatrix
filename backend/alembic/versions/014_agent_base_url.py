"""Add an optional OpenAI-compatible API base URL to agents."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "014_agent_base_url"
down_revision: Union[str, None] = "013_agent_api_key_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("base_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "base_url")
