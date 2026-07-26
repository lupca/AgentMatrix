"""002_add_v2_tables

Revision ID: 002_add_v2_tables
Revises: 001_initial
Create Date: 2026-07-26 14:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '002_add_v2_tables'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    if 'projects' not in tables:
        op.create_table(
            'projects',
            sa.Column('id', sa.String(length=50), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='active'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )

    if 'agents' not in tables:
        op.create_table(
            'agents',
            sa.Column('id', sa.String(length=50), nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('role', sa.String(length=50), nullable=False),
            sa.Column('capabilities', sa.JSON(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='idle'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )

    if 'knowledge_items' not in tables:
        op.create_table(
            'knowledge_items',
            sa.Column('id', sa.String(length=50), nullable=False),
            sa.Column('title', sa.String(length=200), nullable=False),
            sa.Column('category', sa.String(length=50), nullable=True, server_default='general'),
            sa.Column('content', sa.Text(), nullable=False, server_default=''),
            sa.Column('tags', sa.JSON(), nullable=True),
            sa.Column('project', sa.String(length=50), nullable=True),
            sa.Column('author', sa.String(length=50), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index('idx_knowledge_category', 'knowledge_items', ['category'])
        op.create_index('idx_knowledge_project', 'knowledge_items', ['project'])


def downgrade() -> None:
    op.drop_index('idx_knowledge_project', table_name='knowledge_items', if_exists=True)
    op.drop_index('idx_knowledge_category', table_name='knowledge_items', if_exists=True)
    op.drop_table('knowledge_items', if_exists=True)
    op.drop_table('agents', if_exists=True)
    op.drop_table('projects', if_exists=True)
