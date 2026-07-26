"""Add coordinator defaults and seed the built-in coordinator agents."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "010_coordinator_agents"
down_revision: Union[str, None] = "009_add_context_md"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COORDINATORS = (
    {
        "id": "claude-sonnet",
        "name": "Claude Sonnet",
        "model": "claude-sonnet-4-20250514",
        "cli": "claude",
        "is_default": True,
    },
    {
        "id": "claude-opus",
        "name": "Claude Opus",
        "model": "claude-opus-4-5-20251101",
        "cli": "claude",
        "is_default": False,
    },
    {
        "id": "gemini-pro",
        "name": "Gemini Pro",
        "model": "gemini-2.5-pro",
        "cli": "agy",
        "is_default": False,
    },
    {
        "id": "gemini-flash",
        "name": "Gemini Flash",
        "model": "gemini-2.5-flash",
        "cli": "agy",
        "is_default": False,
    },
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    agent_columns = {column["name"] for column in inspector.get_columns("agents")}
    if "is_default" not in agent_columns:
        op.add_column(
            "agents",
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    bind.execute(sa.text("UPDATE agents SET is_default = :is_default"), {"is_default": False})
    for coordinator in COORDINATORS:
        existing = bind.execute(
            sa.text("SELECT id FROM agents WHERE id = :id"),
            {"id": coordinator["id"]},
        ).first()
        values = {
            **coordinator,
            "role": "coordinator",
            "status": "idle",
            "capabilities": "[]",
        }
        if existing:
            bind.execute(
                sa.text(
                    "UPDATE agents SET name = :name, role = :role, model = :model, "
                    "cli = :cli, status = :status, is_default = :is_default "
                    "WHERE id = :id"
                ),
                values,
            )
        else:
            bind.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, name, role, capabilities, status, model, cli, is_default, "
                    "created_at, updated_at) "
                    "VALUES (:id, :name, :role, :capabilities, :status, :model, :cli, "
                    ":is_default, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                values,
            )


def downgrade() -> None:
    op.drop_column("agents", "is_default")
