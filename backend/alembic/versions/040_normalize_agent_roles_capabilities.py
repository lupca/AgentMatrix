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
    "coordinator", "spec_plan", "execute", "python", "react", "typescript", "general",
)


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # Create ENUM types using raw SQL to avoid SQLAlchemy double-create issue
        roles_sql = "CREATE TYPE agent_role AS ENUM (" + ", ".join(f"'{r}'" for r in ROLES) + ")"
        caps_sql = "CREATE TYPE agent_capability AS ENUM (" + ", ".join(f"'{c}'" for c in CAPABILITIES) + ")"
        bind.execute(sa.text(roles_sql))
        bind.execute(sa.text(caps_sql))

        # Create tables with String columns that reference the ENUM
        bind.execute(sa.text("""
            CREATE TABLE role_types (
                role agent_role PRIMARY KEY
            )
        """))
        bind.execute(sa.text("""
            CREATE TABLE capability_types (
                capability agent_capability PRIMARY KEY
            )
        """))
        bind.execute(sa.text("""
            CREATE TABLE agent_roles (
                agent_id VARCHAR(50) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                role agent_role NOT NULL REFERENCES role_types(role),
                PRIMARY KEY (agent_id, role)
            )
        """))
        bind.execute(sa.text("""
            CREATE TABLE agent_capabilities (
                agent_id VARCHAR(50) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                capability agent_capability NOT NULL REFERENCES capability_types(capability),
                PRIMARY KEY (agent_id, capability)
            )
        """))
    else:
        # SQLite fallback - use String columns
        op.create_table(
            "role_types",
            sa.Column("role", sa.String(50), nullable=False),
            sa.PrimaryKeyConstraint("role"),
        )
        op.create_table(
            "capability_types",
            sa.Column("capability", sa.String(100), nullable=False),
            sa.PrimaryKeyConstraint("capability"),
        )
        op.create_table(
            "agent_roles",
            sa.Column("agent_id", sa.String(50), nullable=False),
            sa.Column("role", sa.String(50), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["role"], ["role_types.role"]),
            sa.PrimaryKeyConstraint("agent_id", "role"),
        )
        op.create_table(
            "agent_capabilities",
            sa.Column("agent_id", sa.String(50), nullable=False),
            sa.Column("capability", sa.String(100), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["capability"], ["capability_types.capability"]),
            sa.PrimaryKeyConstraint("agent_id", "capability"),
        )

    # Seed lookup tables
    for role in ROLES:
        bind.execute(sa.text("INSERT INTO role_types (role) VALUES (:role)"), {"role": role})
    for cap in CAPABILITIES:
        bind.execute(sa.text("INSERT INTO capability_types (capability) VALUES (:cap)"), {"cap": cap})

    # Migrate existing data
    agents = sa.table(
        "agents", sa.column("id", sa.String()), sa.column("role", sa.String()),
        sa.column("capabilities", sa.JSON()),
    )
    valid_roles = set(ROLES)
    valid_capabilities = set(CAPABILITIES)

    for row in bind.execute(sa.select(agents)).mappings():
        agent_id = row["id"]

        # Migrate role
        if row["role"] in valid_roles:
            bind.execute(
                sa.text("INSERT INTO agent_roles (agent_id, role) VALUES (:aid, :role)"),
                {"aid": agent_id, "role": row["role"]}
            )

        # Migrate capabilities
        values = row["capabilities"] or []
        if isinstance(values, str):
            try:
                values = json.loads(values)
            except json.JSONDecodeError:
                values = []

        seen = set()
        for cap in (values if isinstance(values, list) else []):
            if cap in valid_capabilities and cap not in seen:
                seen.add(cap)
                bind.execute(
                    sa.text("INSERT INTO agent_capabilities (agent_id, capability) VALUES (:aid, :cap)"),
                    {"aid": agent_id, "cap": cap}
                )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("agent_capabilities")
    op.drop_table("agent_roles")
    op.drop_table("capability_types")
    op.drop_table("role_types")
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP TYPE IF EXISTS agent_capability"))
        bind.execute(sa.text("DROP TYPE IF EXISTS agent_role"))
