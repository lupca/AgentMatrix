"""Add atomic event sequence counter to agent_runs.

Fixes race condition in _next_agent_event_seq where concurrent processes
could get the same sequence number, causing UniqueViolation on insert.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "045_atomic_event_seq"
down_revision: Union[str, None] = "044_knowledge_items_embedding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add atomic counter column
    op.add_column(
        "agent_runs",
        sa.Column("next_event_seq", sa.Integer(), nullable=False, server_default="0"),
    )

    # Backfill from existing agent_events
    op.execute("""
        UPDATE agent_runs ar
        SET next_event_seq = COALESCE(
            (SELECT MAX(seq) + 1 FROM agent_events ae WHERE ae.run_id = ar.id),
            0
        )
    """)


def downgrade() -> None:
    op.drop_column("agent_runs", "next_event_seq")
