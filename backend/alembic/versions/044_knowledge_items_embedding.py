"""Add embedding column to knowledge_items for pgvector semantic search."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "044_knowledge_items_embedding"
down_revision: Union[str, None] = "043_fix_verdict_trigger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # pgvector extension should already exist from migration 042
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute("ALTER TABLE knowledge_items ADD COLUMN embedding vector(2560)")
    else:
        # SQLite fallback for tests
        op.add_column("knowledge_items", sa.Column("embedding", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_items", "embedding")
