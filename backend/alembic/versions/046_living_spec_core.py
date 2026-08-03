"""Create the living-spec core tables.

This is intentionally the only migration in the CTV2-1341 batch.  Keep its
parent at 045_atomic_event_seq so Alembic remains a single linear history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "046_living_spec_core"
down_revision: Union[str, None] = "045_atomic_event_seq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SPEC_KINDS = ("requirement", "decision", "constraint", "interface", "design")
SPEC_STATUSES = ("draft", "active", "stale", "superseded")
SPEC_CONFIDENCES = ("asserted", "derived", "verified")
RELATION_KINDS = ("conflicts_with", "duplicates", "refines", "depends_on")


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "spec_item",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(50),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column(
            "supersedes_id",
            sa.String(36),
            sa.ForeignKey("spec_item.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_doc_id", sa.String(50), nullable=True),
        sa.Column("derived_from_sha", sa.String(64), nullable=True),
        sa.Column("derived_by", sa.String(100), nullable=True),
        sa.Column("confidence", sa.String(20), nullable=False, server_default="asserted"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_by", sa.String(100), nullable=True),
        # SQLite uses Text; PostgreSQL is converted to pgvector immediately
        # below after the extension is available.
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "kind IN ('requirement', 'decision', 'constraint', 'interface', 'design')",
            name="ck_spec_items_kind",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'stale', 'superseded')",
            name="ck_spec_items_status",
        ),
        sa.CheckConstraint(
            "confidence IN ('asserted', 'derived', 'verified')",
            name="ck_spec_items_confidence",
        ),
    )
    op.create_index("ix_spec_item_project_id", "spec_item", ["project_id"])
    op.create_index("ix_spec_item_kind", "spec_item", ["kind"])
    op.create_index("ix_spec_item_status", "spec_item", ["status"])
    op.create_index("ix_spec_item_supersedes_id", "spec_item", ["supersedes_id"])
    op.create_index("ix_spec_item_source_doc_id", "spec_item", ["source_doc_id"])
    op.create_index("ix_spec_item_archived_at", "spec_item", ["archived_at"])
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE spec_item ALTER COLUMN embedding TYPE vector(1536) "
            "USING embedding::vector"
        )

    op.create_table(
        "spec_relation",
        sa.Column(
            "from_id",
            sa.String(36),
            sa.ForeignKey("spec_item.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "to_id",
            sa.String(36),
            sa.ForeignKey("spec_item.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("kind", sa.String(20), primary_key=True),
        sa.CheckConstraint(
            "kind IN ('conflicts_with', 'duplicates', 'refines', 'depends_on')",
            name="ck_spec_relations_kind",
        ),
    )
    op.create_index("ix_spec_relation_from_id", "spec_relation", ["from_id"])
    op.create_index("ix_spec_relation_to_id", "spec_relation", ["to_id"])


def downgrade() -> None:
    op.drop_index("ix_spec_relation_to_id", table_name="spec_relation")
    op.drop_index("ix_spec_relation_from_id", table_name="spec_relation")
    op.drop_table("spec_relation")
    op.drop_index("ix_spec_item_archived_at", table_name="spec_item")
    op.drop_index("ix_spec_item_source_doc_id", table_name="spec_item")
    op.drop_index("ix_spec_item_supersedes_id", table_name="spec_item")
    op.drop_index("ix_spec_item_status", table_name="spec_item")
    op.drop_index("ix_spec_item_kind", table_name="spec_item")
    op.drop_index("ix_spec_item_project_id", table_name="spec_item")
    op.drop_table("spec_item")
