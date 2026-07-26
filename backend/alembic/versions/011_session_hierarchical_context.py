"""Add hierarchical context fields to sessions (global/project/task).

Revision ID: 011_session_hierarchical_context
Revises: 010_coordinator_agents
Create Date: 2026-07-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "011_session_hierarchical_context"
down_revision: Union[str, None] = "010_coordinator_agents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Columns first (no constraints yet) so the backfill below can read/write them.
    # Grouped in a batch so SQLite (used by the test suite) recreates the table once
    # instead of failing on unsupported inline ALTER TABLE ... ADD COLUMN + FK.
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("context_level", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("title", sa.String(length=200), nullable=True))
        batch_op.add_column(
            sa.Column("status", sa.String(length=10), nullable=True, server_default="active")
        )
        batch_op.add_column(
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default="false")
        )
        batch_op.add_column(
            sa.Column("message_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "last_activity_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            )
        )

    # Backfill: sessions with task_id -> context_level='task', project_id derived from
    # the task's project; sessions without task_id -> context_level='global'.
    # A task_id whose task references a project row that no longer exists cannot
    # satisfy the FK/consistency constraints below, so those legacy sessions are
    # demoted to context_level='global' (task_id cleared) rather than left dangling.
    op.execute(
        """
        UPDATE sessions
        SET project_id = (
            SELECT t.project
            FROM tasks t
            JOIN projects p ON p.id = t.project
            WHERE t.id = sessions.task_id
        )
        WHERE sessions.task_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE sessions
        SET
            context_level = CASE
                WHEN sessions.task_id IS NOT NULL AND sessions.project_id IS NOT NULL THEN 'task'
                ELSE 'global'
            END,
            task_id = CASE
                WHEN sessions.task_id IS NOT NULL AND sessions.project_id IS NULL THEN NULL
                ELSE sessions.task_id
            END,
            status = COALESCE(sessions.status, 'active'),
            message_count = COALESCE(json_array_length(sessions.messages), 0),
            last_activity_at = COALESCE(sessions.updated_at, sessions.created_at, CURRENT_TIMESTAMP)
        """
    )

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.alter_column("context_level", nullable=False)
        batch_op.alter_column("status", nullable=False)
        batch_op.alter_column("last_activity_at", nullable=False)
        batch_op.create_foreign_key(
            "fk_sessions_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_sessions_context_level_valid",
            "context_level IN ('global', 'project', 'task')",
        )
        batch_op.create_check_constraint(
            "ck_sessions_status_valid",
            "status IN ('active', 'archived', 'closed')",
        )
        batch_op.create_check_constraint(
            "ck_sessions_task_requires_project",
            "(task_id IS NULL) OR (project_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_sessions_context_level_consistency",
            "(context_level = 'global' AND project_id IS NULL AND task_id IS NULL) OR "
            "(context_level = 'project' AND project_id IS NOT NULL AND task_id IS NULL) OR "
            "(context_level = 'task' AND project_id IS NOT NULL AND task_id IS NOT NULL)",
        )
        batch_op.create_index("ix_sessions_project_id", ["project_id"])
        batch_op.create_index("ix_sessions_status", ["status"])
        batch_op.create_index(
            "ix_sessions_context_listing",
            ["context_level", "project_id", "status", "last_activity_at"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_index("ix_sessions_context_listing")
        batch_op.drop_index("ix_sessions_status")
        batch_op.drop_index("ix_sessions_project_id")
        batch_op.drop_constraint("ck_sessions_context_level_consistency", type_="check")
        batch_op.drop_constraint("ck_sessions_task_requires_project", type_="check")
        batch_op.drop_constraint("ck_sessions_status_valid", type_="check")
        batch_op.drop_constraint("ck_sessions_context_level_valid", type_="check")
        batch_op.drop_constraint("fk_sessions_project_id_projects", type_="foreignkey")
        batch_op.drop_column("last_activity_at")
        batch_op.drop_column("message_count")
        batch_op.drop_column("pinned")
        batch_op.drop_column("status")
        batch_op.drop_column("title")
        batch_op.drop_column("context_level")
        batch_op.drop_column("project_id")
