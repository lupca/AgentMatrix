"""Create agents_view with roles/capabilities arrays.

Revision ID: 041_create_agents_view
Revises: 040_normalize_agent_roles_capabilities
Create Date: 2026-08-02
"""
from alembic import op
import sqlalchemy as sa

revision = "041_create_agents_view"
down_revision = "040_normalize_agent_roles_capabilities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("""
            CREATE OR REPLACE VIEW agents_view AS
            SELECT
                a.id, a.name, a.role, a.status, a.agent_type, a.model, a.effort,
                a.cli, a.provider, a.base_url, a.is_default, a.success_rate,
                a.created_at, a.updated_at, a.archived_at,
                COALESCE(array_agg(DISTINCT ar.role) FILTER (WHERE ar.role IS NOT NULL), '{}') as roles,
                COALESCE(array_agg(DISTINCT ac.capability) FILTER (WHERE ac.capability IS NOT NULL), '{}') as capabilities_array
            FROM agents a
            LEFT JOIN agent_roles ar ON a.id = ar.agent_id
            LEFT JOIN agent_capabilities ac ON a.id = ac.agent_id
            GROUP BY a.id
        """))
        bind.execute(sa.text("GRANT SELECT ON agents_view TO ct_readonly_user"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP VIEW IF EXISTS agents_view"))
