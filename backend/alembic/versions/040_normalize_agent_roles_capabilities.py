"""Normalize agent roles and capabilities into validated lookup/junction tables."""

from typing import Sequence, Union
import json

import sqlalchemy as sa
from alembic import op

revision: str = "040_normalize_agent_roles_capabilities"
down_revision: Union[str, None] = "039_inbox_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLES = ("executor", "reviewer", "coordinator", "spec_plan")
CAPABILITIES = (
    "code", "backend", "frontend", "review", "research", "architecture", "testing",
    "coordination", "devops", "infra", "api", "database", "documentation", "reasoning",
    "verification", "diff-reading", "test-running", "complex-backend", "complex-frontend",
    "complex-analysis", "complex-logic", "complex-refactor", "simple-tasks", "fast",
    "fast-execution", "fast-iteration", "full-implementation", "cleanup", "markdown-cleanup",
    "skill-design", "skills", "spec-planning", "decomposition", "graph-sourcing",
    "audit-logging", "spot-check-runtime", "ac-generation", "process-design", "confirmation",
    "creative", "deep-research", "final-decision", "follows-explicit-instructions",
    "follows-instructions", "reliable", "code-review",
    # Existing values observed in legacy agent JSON profiles.
    "coordinator", "spec_plan", "execute", "python", "react", "typescript", "general",
)


def _enum(name: str, values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _enum("agent_role", ROLES).create(bind, checkfirst=True)
        _enum("agent_capability", CAPABILITIES).create(bind, checkfirst=True)

    op.create_table(
        "role_types",
        sa.Column("role", _enum("agent_role", ROLES), nullable=False),
        sa.PrimaryKeyConstraint("role"),
    )
    op.create_table(
        "capability_types",
        sa.Column("capability", _enum("agent_capability", CAPABILITIES), nullable=False),
        sa.PrimaryKeyConstraint("capability"),
    )
    op.create_table(
        "agent_roles",
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("role", _enum("agent_role", ROLES), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role"], ["role_types.role"]),
        sa.PrimaryKeyConstraint("agent_id", "role"),
    )
    op.create_table(
        "agent_capabilities",
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("capability", _enum("agent_capability", CAPABILITIES), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["capability"], ["capability_types.capability"]),
        sa.PrimaryKeyConstraint("agent_id", "capability"),
    )

    op.bulk_insert(
        sa.table("role_types", sa.column("role", sa.String())),
        [{"role": role} for role in ROLES],
    )
    op.bulk_insert(
        sa.table("capability_types", sa.column("capability", sa.String())),
        [{"capability": capability} for capability in CAPABILITIES],
    )

    # Only recognized enum values are copied into the canonical tables. The
    # deprecated JSON columns remain intact, so an installation can audit or
    # remediate legacy values before a future cleanup migration. Reading rows
    # through SQLAlchemy keeps this data migration valid on PostgreSQL and in
    # lightweight SQLite migration tests alike.
    agents = sa.table(
        "agents", sa.column("id", sa.String()), sa.column("role", sa.String()),
        sa.column("capabilities", sa.JSON()),
    )
    role_rows: list[dict[str, str]] = []
    capability_rows: list[dict[str, str]] = []
    valid_roles = set(ROLES)
    valid_capabilities = set(CAPABILITIES)
    for row in bind.execute(sa.select(agents)).mappings():
        if row["role"] in valid_roles:
            role_rows.append({"agent_id": row["id"], "role": row["role"]})
        values = row["capabilities"] or []
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except json.JSONDecodeError:
                values = []
        for capability in dict.fromkeys(values if isinstance(values, list) else []):
            if capability in valid_capabilities:
                capability_rows.append({"agent_id": row["id"], "capability": capability})
    if role_rows:
        op.bulk_insert(
            sa.table("agent_roles", sa.column("agent_id", sa.String()), sa.column("role", sa.String())),
            role_rows,
        )
    if capability_rows:
        op.bulk_insert(
            sa.table("agent_capabilities", sa.column("agent_id", sa.String()), sa.column("capability", sa.String())),
            capability_rows,
        )


def downgrade() -> None:
    op.drop_table("agent_capabilities")
    op.drop_table("agent_roles")
    op.drop_table("capability_types")
    op.drop_table("role_types")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        _enum("agent_capability", CAPABILITIES).drop(bind, checkfirst=True)
        _enum("agent_role", ROLES).drop(bind, checkfirst=True)
