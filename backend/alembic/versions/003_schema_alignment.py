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
        op.add_column('projects', sa.Column('repo_root', sa.String(length=255), nullable=True))
        op.add_column('projects', sa.Column('task_prefix', sa.String(length=20), nullable=True))
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
        op.add_column('agents', sa.Column('type', sa.String(length=50), nullable=True))
        op.add_column('agents', sa.Column('model', sa.String(length=50), nullable=True))
        op.add_column('agents', sa.Column('effort', sa.String(length=20), nullable=True))
        op.add_column('agents', sa.Column('cli', sa.String(length=50), nullable=True))
        op.add_column('agents', sa.Column('success_rate', sa.Float(), nullable=True, server_default='0.0'))

    try:
        op.create_foreign_key('fk_tasks_project', 'tasks', 'projects', ['project'], ['id'])
    except Exception:
        pass


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
