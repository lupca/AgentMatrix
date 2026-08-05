"""Make task_events.task_id nullable (CTV2-1400).

`ask_human` accepts an optional `task_id` -- a coordinator can ask a human
about something that is not tied to any single task row. `TaskEvent.task_id`
was NOT NULL, so a task-less `human_question` event had nowhere to be
recorded. Relaxes the FK to nullable; ON DELETE CASCADE only applies when a
task_id is present.

Not applied to the real DB as part of this change (per instructions) --
`alembic heads` should show exactly one head, 057_task_events_task_id_nullable,
immediately after 056_task_owners.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "057_task_events_task_id_nullable"
down_revision: str | None = "056_task_owners"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_events") as batch_op:
        batch_op.alter_column(
            "task_id", existing_type=sa.String(20), nullable=True
        )
    with op.batch_alter_table("notification_deliveries") as batch_op:
        batch_op.alter_column(
            "task_id", existing_type=sa.String(20), nullable=True
        )


def downgrade() -> None:
    with op.batch_alter_table("task_events") as batch_op:
        batch_op.alter_column(
            "task_id", existing_type=sa.String(20), nullable=False
        )
    with op.batch_alter_table("notification_deliveries") as batch_op:
        batch_op.alter_column(
            "task_id", existing_type=sa.String(20), nullable=False
        )
