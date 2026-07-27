"""Add tasks.legacy_no_ac so pre-existing AC-less tasks aren't blocked by the
new spec/plan dispatch gate (CTV2-091).

Revision ID: 019_legacy_no_ac
Revises: 018_agent_run_kind
Create Date: 2026-07-27 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "019_legacy_no_ac"
down_revision: Union[str, None] = "018_agent_run_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(
            sa.Column(
                "legacy_no_ac", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    # Backfill: any task that already exists without acceptance_criteria is
    # exempted from the new "dispatch requires AC" gate, so the in-flight
    # backlog isn't stuck waiting on a spec/plan run it never had. Loaded and
    # filtered in Python (rather than a raw JSON predicate) to stay correct
    # across both SQLite and Postgres JSON representations.
    tasks = sa.table(
        "tasks",
        sa.column("id", sa.String),
        sa.column("acceptance_criteria", sa.JSON),
        sa.column("legacy_no_ac", sa.Boolean),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(tasks.c.id, tasks.c.acceptance_criteria)).fetchall()
    empty_ids = [row.id for row in rows if not row.acceptance_criteria]
    if empty_ids:
        bind.execute(
            tasks.update().where(tasks.c.id.in_(empty_ids)).values(legacy_no_ac=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("legacy_no_ac")
