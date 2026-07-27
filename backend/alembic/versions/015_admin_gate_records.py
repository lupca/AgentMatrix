"""Add admin_gate_records ledger and knowledge_items.status (ADR-001 §D2, CTV2-082).

Revision ID: 015_admin_gate_records
Revises: 014_agent_base_url
Create Date: 2026-07-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "015_admin_gate_records"
down_revision: Union[str, None] = "014_agent_base_url"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_gate_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("actor", sa.String(length=50), nullable=False, server_default="system"),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="supervised"),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey("admin_gate_records.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("input_payload", sa.JSON(), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "entity IN ('projects', 'agents')", name="ck_admin_gate_records_entity"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_admin_gate_records_status",
        ),
        sa.CheckConstraint(
            "mode IN ('supervised', 'bypass')", name="ck_admin_gate_records_mode"
        ),
    )
    op.create_index(
        "ix_admin_gate_records_entity", "admin_gate_records", ["entity"]
    )
    op.create_index(
        "ix_admin_gate_records_entity_id", "admin_gate_records", ["entity_id"]
    )
    op.create_index(
        "ix_admin_gate_records_parent_id", "admin_gate_records", ["parent_id"]
    )

    op.add_column(
        "knowledge_items",
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="active"
        ),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION ct_admin_gate_records_immutable() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'admin_gate_records is an append-only ledger';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_admin_gate_records_immutable
            BEFORE UPDATE OR DELETE ON admin_gate_records
            FOR EACH ROW EXECUTE FUNCTION ct_admin_gate_records_immutable()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_admin_gate_records_immutable ON admin_gate_records"
        )
        op.execute("DROP FUNCTION IF EXISTS ct_admin_gate_records_immutable()")

    op.drop_column("knowledge_items", "status")

    op.drop_index("ix_admin_gate_records_parent_id", table_name="admin_gate_records")
    op.drop_index("ix_admin_gate_records_entity_id", table_name="admin_gate_records")
    op.drop_index("ix_admin_gate_records_entity", table_name="admin_gate_records")
    op.drop_table("admin_gate_records")
