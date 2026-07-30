"""Add TaskEvent v2 kind, claims, and per-session digest cursors (CTV2-132)."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "033_task_event_schema_v2"
down_revision: Union[str, None] = "032_add_project_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite cannot ALTER TABLE to add constraints; batch mode rebuilds
        # the table while preserving existing rows for the backfill below.
        with op.batch_alter_table("task_events", recreate="always") as batch:
            batch.add_column(
                sa.Column("kind", sa.String(length=10), nullable=False, server_default="info")
            )
            batch.add_column(
                sa.Column("claimed_by_session_id", sa.String(length=36), nullable=True)
            )
            batch.create_foreign_key(
                "fk_task_events_claimed_session",
                "sessions",
                ["claimed_by_session_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch.create_check_constraint(
                "ck_task_events_kind_valid", "kind IN ('decision', 'info')"
            )
    else:
        op.add_column(
            "task_events",
            sa.Column("kind", sa.String(length=10), nullable=False, server_default="info"),
        )
        op.add_column(
            "task_events",
            sa.Column(
                "claimed_by_session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_check_constraint(
            "ck_task_events_kind_valid",
            "task_events",
            "kind IN ('decision', 'info')",
        )
    op.create_index(
        "idx_task_events_decision_claim",
        "task_events",
        ["kind", "claimed_by_session_id"],
        postgresql_where=sa.text("kind = 'decision'"),
        sqlite_where=sa.text("kind = 'decision'"),
    )
    # Some deployments run Base.metadata.create_all() before Alembic and may
    # already have this new table while still being stamped at revision 032.
    # Adopt that model-created table instead of failing the migration.
    if not sa.inspect(op.get_bind()).has_table("session_event_cursors"):
        op.create_table(
            "session_event_cursors",
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sessions.id", ondelete="CASCADE"),
                primary_key=True,
                nullable=False,
            ),
            sa.Column(
                "last_digest_event_id",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    op.execute(
        "UPDATE task_events SET kind = 'decision' "
        "WHERE event_type IN ('gate_pending', 'run_failed', 'escalated')"
    )


def downgrade() -> None:
    op.drop_table("session_event_cursors")
    op.drop_index("idx_task_events_decision_claim", table_name="task_events")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("task_events", recreate="always") as batch:
            batch.drop_constraint("ck_task_events_kind_valid", type_="check")
            batch.drop_constraint("fk_task_events_claimed_session", type_="foreignkey")
            batch.drop_column("claimed_by_session_id")
            batch.drop_column("kind")
    else:
        op.drop_constraint("ck_task_events_kind_valid", "task_events", type_="check")
        op.drop_column("task_events", "claimed_by_session_id")
        op.drop_column("task_events", "kind")
