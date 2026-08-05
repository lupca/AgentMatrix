"""Add spec_item.realization (CTV2-1395).

Independent axis from `status`: whether a claim has become code, not whether
the claim is still correct. See docs/spec/08-living-spec.md "Truc THUC HOA".
Always defaults to 'agreed'; the column is never written by spec_write --
`spec_get` derives the live agreed/built projection on every read from
spec_anchor + spec_task_link + task status. `alembic heads` showed exactly
one head, 052_notification_deliveries, immediately before this file was
authored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "053_spec_item_realization"
down_revision: str | None = "052_notification_deliveries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch mode: SQLite (used by the migration-cycle tests) cannot ALTER
    # constraints in place -- batch rebuilds the table there, while Postgres
    # gets a plain ADD COLUMN + ADD CONSTRAINT.
    with op.batch_alter_table("spec_item") as batch:
        batch.add_column(
            sa.Column("realization", sa.String(10), nullable=False, server_default="agreed")
        )
        batch.create_check_constraint(
            "ck_spec_item_realization", "realization IN ('agreed', 'built')"
        )


def downgrade() -> None:
    with op.batch_alter_table("spec_item") as batch:
        batch.drop_constraint("ck_spec_item_realization", type_="check")
        batch.drop_column("realization")
