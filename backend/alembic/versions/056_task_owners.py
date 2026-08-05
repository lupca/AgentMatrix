"""Add task_owners (CTV2-1399).

Ai động vào task (create/update/dispatch/review/approve/verdict/land/reopen/
cancel/spec_write) qua MCP thì thành chủ task đó -- last-writer-wins, một
dòng mỗi task. Dùng để đẩy tin điều phối đúng người thay vì đẩy đại trà mọi
phiên. session_id NULL hoặc phiên không còn hoạt động (last_activity_at quá
hạn) nghĩa là "vô chủ" -- hiện cho mọi phiên.

`alembic heads` showed exactly one head, 055_review_cycles, immediately
before this file was authored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "056_task_owners"
down_revision: str | None = "055_review_cycles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_owners",
        sa.Column("task_id", sa.String(20), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_task_owners_session_id", "task_owners", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_task_owners_session_id", table_name="task_owners")
    op.drop_table("task_owners")
