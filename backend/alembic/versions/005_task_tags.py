"""Add task tags used by agent-task matching."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_task_tags"
down_revision: Union[str, None] = "004_agent_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tags" not in {column["name"] for column in inspector.get_columns("tasks")}:
        op.add_column("tasks", sa.Column("tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "tags")
