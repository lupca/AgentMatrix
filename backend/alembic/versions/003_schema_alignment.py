"""003_schema_alignment

Revision ID: 003_schema_alignment
Revises: 001_initial
Create Date: 2026-07-26 15:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '003_schema_alignment'
down_revision: Union[str, None] = '002_add_v2_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'projects' not in tables:
        op.create_table(
            'projects',
            sa.Column('id', sa.String(length=50), primary_key=True),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('repo_root', sa.String(length=255), nullable=True),
            sa.Column('task_prefix', sa.String(length=20), nullable=True),
            sa.Column('graph_status', sa.String(length=20), nullable=True, server_default='idle')
        )
    else:
        project_cols = [c['name'] for c in inspector.get_columns('projects')]
        if 'repo_root' not in project_cols:
            op.add_column('projects', sa.Column('repo_root', sa.String(length=255), nullable=True))
        if 'task_prefix' not in project_cols:
            op.add_column('projects', sa.Column('task_prefix', sa.String(length=20), nullable=True))
        if 'graph_status' not in project_cols:
            op.add_column('projects', sa.Column('graph_status', sa.String(length=20), nullable=True, server_default='idle'))

    if 'agents' not in tables:
        op.create_table(
            'agents',
            sa.Column('id', sa.String(length=50), primary_key=True),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('role', sa.String(length=50), nullable=True),
            sa.Column('type', sa.String(length=50), nullable=True),
            sa.Column('model', sa.String(length=50), nullable=True),
            sa.Column('effort', sa.String(length=20), nullable=True),
            sa.Column('cli', sa.String(length=50), nullable=True),
            sa.Column('success_rate', sa.Float(), nullable=True, server_default='0.0')
        )
    else:
        agent_cols = [c['name'] for c in inspector.get_columns('agents')]
        if 'type' not in agent_cols:
            op.add_column('agents', sa.Column('type', sa.String(length=50), nullable=True))
        if 'model' not in agent_cols:
            op.add_column('agents', sa.Column('model', sa.String(length=50), nullable=True))
        if 'effort' not in agent_cols:
            op.add_column('agents', sa.Column('effort', sa.String(length=20), nullable=True))
        if 'cli' not in agent_cols:
            op.add_column('agents', sa.Column('cli', sa.String(length=50), nullable=True))
        if 'success_rate' not in agent_cols:
            op.add_column('agents', sa.Column('success_rate', sa.Float(), nullable=True, server_default='0.0'))

    if conn.dialect.name != 'sqlite':
        fks = [fk.get('name') for fk in inspector.get_foreign_keys('tasks')]
        if 'fk_tasks_project' not in fks:
            orphans = conn.execute(sa.text("SELECT COUNT(*) FROM tasks WHERE project NOT IN (SELECT id FROM projects)")).scalar()
            if orphans == 0:
                op.create_foreign_key('fk_tasks_project', 'tasks', 'projects', ['project'], ['id'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    try:
        op.drop_constraint('fk_tasks_project', 'tasks', type_='foreignkey')
    except Exception:
        pass

    if 'agents' in tables:
        op.drop_table('agents')

    if 'projects' in tables:
        op.drop_table('projects')
