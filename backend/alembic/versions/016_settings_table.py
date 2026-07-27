"""Add settings KV table (ADR-001 §D2 Phase 2d, CTV2-083).

Revision ID: 016_settings_table
Revises: 015_admin_gate_records
Create Date: 2026-07-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "016_settings_table"
down_revision: Union[str, None] = "015_admin_gate_records"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
    )

    with op.batch_alter_table("admin_gate_records") as batch_op:
        batch_op.drop_constraint("ck_admin_gate_records_entity", type_="check")
        batch_op.create_check_constraint(
            "ck_admin_gate_records_entity",
            "entity IN ('projects', 'agents', 'settings')",
        )


def downgrade() -> None:
    with op.batch_alter_table("admin_gate_records") as batch_op:
        batch_op.drop_constraint("ck_admin_gate_records_entity", type_="check")
        batch_op.create_check_constraint(
            "ck_admin_gate_records_entity",
            "entity IN ('projects', 'agents')",
        )

    op.drop_table("settings")
