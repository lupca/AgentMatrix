"""Add agent notes with project/task many-to-many links and pgvector search."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "042_agent_notes"
down_revision: Union[str, None] = "041_create_agents_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOTE_TYPES = ("fact", "decision", "observation", "procedure", "preference")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        op.execute(
            "CREATE TYPE note_type AS ENUM (" + ", ".join(f"'{value}'" for value in NOTE_TYPES) + ")"
        )
        op.execute("""
            CREATE TABLE agent_notes (
                id VARCHAR(36) PRIMARY KEY,
                title VARCHAR(300) NOT NULL,
                content TEXT NOT NULL,
                note_type note_type NOT NULL DEFAULT 'fact',
                tags JSONB NOT NULL DEFAULT '[]'::jsonb,
                embedding vector(2560),
                author VARCHAR(50),
                archived_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
    else:
        op.create_table(
            "agent_notes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("note_type", sa.String(30), nullable=False, server_default="fact"),
            sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("embedding", sa.Text(), nullable=True),
            sa.Column("author", sa.String(50)),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    op.create_table(
        "note_projects",
        sa.Column("note_id", sa.String(36), sa.ForeignKey("agent_notes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("project_id", sa.String(50), sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "note_tasks",
        sa.Column("note_id", sa.String(36), sa.ForeignKey("agent_notes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("task_id", sa.String(20), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_agent_notes_note_type", "agent_notes", ["note_type"])
    op.create_index("ix_agent_notes_archived_at", "agent_notes", ["archived_at"])
    if bind.dialect.name == "postgresql":
        # Note: ivfflat/hnsw indexes don't support > 2000 dimensions
        # Qwen3-Embedding-4B outputs 2560 dims, so no vector index for now
        op.execute("""
            DO $$ BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ct_readonly_user') THEN
                    GRANT SELECT ON agent_notes, note_projects, note_tasks TO ct_readonly_user;
                END IF;
            END $$;
        """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_agent_notes_embedding")
    op.drop_index("ix_agent_notes_archived_at", table_name="agent_notes")
    op.drop_index("ix_agent_notes_note_type", table_name="agent_notes")
    op.drop_table("note_tasks")
    op.drop_table("note_projects")
    op.drop_table("agent_notes")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS note_type")
