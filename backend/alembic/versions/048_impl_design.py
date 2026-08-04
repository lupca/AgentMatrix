"""Add the implementation-design artifact (CTV2-1355)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "048_impl_design"
down_revision: Union[str, None] = "047_spec_anchor_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    jsonb = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "impl_design",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(20),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("files", jsonb, nullable=False, server_default="[]"),
        sa.Column("changes", jsonb, nullable=False, server_default="[]"),
        sa.Column("data_changes", jsonb, nullable=False, server_default="[]"),
        sa.Column("test_plan", jsonb, nullable=False, server_default="[]"),
        sa.Column("risks", jsonb, nullable=False, server_default="[]"),
        sa.Column("non_goals", jsonb, nullable=False, server_default="[]"),
        sa.Column("derived_from_sha", sa.String(64), nullable=True),
        sa.Column("authored_by", sa.String(100), nullable=False, server_default="unknown"),
        sa.Column("completeness", jsonb, nullable=True),
        sa.Column("reviewed_by", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", name="uq_impl_design_task_id"),
    )
    op.create_index("ix_impl_design_task_id", "impl_design", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_impl_design_task_id", table_name="impl_design")
    op.drop_table("impl_design")
