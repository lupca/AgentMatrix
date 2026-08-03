"""Add spec_anchor table and spec_item.stale_reason (CTV2-1342).

down_revision points at the merge head created for the CTV2-1341/CTV2-1339
batch (which descends from 046_living_spec_core, the CTV2-1341 revision),
not at 045, so Alembic keeps a single linear history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "047_spec_anchor_core"
down_revision: Union[str, None] = "4e7ab15544a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ANCHOR_RELATIONS = ("implements", "constrains", "tests", "documents")


def upgrade() -> None:
    op.add_column("spec_item", sa.Column("stale_reason", sa.Text(), nullable=True))

    op.create_table(
        "spec_anchor",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "spec_item_id",
            sa.String(36),
            sa.ForeignKey("spec_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("repo", sa.String(255), nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("symbol", sa.String(300), nullable=False),
        sa.Column("relation", sa.String(20), nullable=False),
        sa.Column("anchor_sha", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "relation IN ('implements', 'constrains', 'tests', 'documents')",
            name="ck_spec_anchor_relation",
        ),
        sa.UniqueConstraint(
            "spec_item_id", "repo", "path", "symbol", "relation", name="uq_spec_anchor_target"
        ),
    )
    op.create_index("ix_spec_anchor_spec_item_id", "spec_anchor", ["spec_item_id"])
    op.create_index("ix_spec_anchor_repo_path", "spec_anchor", ["repo", "path"])


def downgrade() -> None:
    op.drop_index("ix_spec_anchor_repo_path", table_name="spec_anchor")
    op.drop_index("ix_spec_anchor_spec_item_id", table_name="spec_anchor")
    op.drop_table("spec_anchor")
    op.drop_column("spec_item", "stale_reason")
