"""Add tasks.coordinator_notes to separate coordinator input from planner output (CTV2-1397).

task.plan was owned by two writers: the planner (its output) and the coordinator
(its reply for the next round). write_spec_plan overwrote the whole cell, silently
discarding any coordinator reply written while a plan run was in flight. This adds
a dedicated, coordinator-only column that the planner reads but never writes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "054_task_coordinator_notes"
down_revision: str | None = "053_spec_item_realization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("coordinator_notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "coordinator_notes")
