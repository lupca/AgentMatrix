"""Add raw idea inbox items."""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "039_inbox_items"
down_revision: Union[str, None] = "038_add_spec_clarity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbox_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("project_id", sa.String(length=50), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_id", sa.String(length=20), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('open', 'triaged', 'dropped')", name="ck_inbox_items_status"),
    )
    op.create_index("ix_inbox_items_status", "inbox_items", ["status"])
    op.create_index("ix_inbox_items_project_id", "inbox_items", ["project_id"])
    # query_db runs as ct_readonly_user; without an explicit grant a fresh
    # machine (no default privileges) cannot read the new table (review F-001).
    if op.get_bind().dialect.name == "postgresql":
        op.execute("GRANT SELECT ON inbox_items TO ct_readonly_user")


def downgrade() -> None:
    op.drop_index("ix_inbox_items_project_id", table_name="inbox_items")
    op.drop_index("ix_inbox_items_status", table_name="inbox_items")
    op.drop_table("inbox_items")
