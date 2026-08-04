"""Link project spec items to the tasks that reference or change them (CTV2-1367)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "049_spec_task_link"
down_revision: Union[str, None] = "048_impl_design"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "spec_task_link",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "spec_item_id",
            sa.String(36),
            sa.ForeignKey("spec_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(20),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(20), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False, server_default="asserted"),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "relation IN ('implements', 'modifies', 'violates', 'references')",
            name="ck_spec_task_link_relation",
        ),
        sa.CheckConstraint(
            "confidence IN ('asserted', 'derived', 'verified')",
            name="ck_spec_task_link_confidence",
        ),
        sa.UniqueConstraint(
            "spec_item_id", "task_id", "relation", name="uq_spec_task_link_edge"
        ),
    )
    op.create_index("ix_spec_task_link_spec_item_id", "spec_task_link", ["spec_item_id"])
    op.create_index("ix_spec_task_link_task_id", "spec_task_link", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_spec_task_link_task_id", table_name="spec_task_link")
    op.drop_index("ix_spec_task_link_spec_item_id", table_name="spec_task_link")
    op.drop_table("spec_task_link")
