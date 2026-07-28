"""Add soft-delete timestamps to user-owned entities."""

from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "022_archivable_entities"
down_revision: Union[str, None] = "021_project_autonomy_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("tasks", "projects", "agents", "knowledge_items", "sessions", "settings")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
            batch_op.create_index(f"ix_{table}_archived_at", ["archived_at"])


def downgrade() -> None:
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_archived_at")
            batch_op.drop_column("archived_at")
