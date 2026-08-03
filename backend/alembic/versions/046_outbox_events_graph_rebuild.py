"""Add graph_rebuild_requested to outbox_events check constraint.

Revision ID: 046_outbox_events_graph_rebuild
Revises: 045_atomic_event_seq
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "046_outbox_events_graph_rebuild"
down_revision: Union[str, None] = "045_atomic_event_seq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type",
        "outbox_events",
        "event_type IN ('run_requested', 'graph_rebuild_requested')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_outbox_events_type", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_events_type",
        "outbox_events",
        "event_type IN ('run_requested')",
    )
