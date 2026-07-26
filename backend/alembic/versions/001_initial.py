"""001_initial

Revision ID: 001_initial
Revises: 
Create Date: 2026-07-26 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'tasks',
        sa.Column('id', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=50), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='todo'),
        sa.Column('priority', sa.String(length=10), nullable=True),
        sa.Column('risk', sa.String(length=10), nullable=True),
        sa.Column('executor', sa.String(length=50), nullable=True),
        sa.Column('reviewer', sa.String(length=50), nullable=True),
        sa.Column('acceptance_criteria', sa.JSON(), nullable=True),
        sa.Column('files', sa.JSON(), nullable=True),
        sa.Column('tests', sa.JSON(), nullable=True),
        sa.Column('flows', sa.JSON(), nullable=True),
        sa.Column('plan', sa.Text(), nullable=True),
        sa.Column('result_ref', sa.String(length=100), nullable=True),
        sa.Column('findings', sa.JSON(), nullable=True),
        sa.Column('verdict', sa.String(length=10), nullable=True),
        sa.Column('predicted_success', sa.String(length=10), nullable=True),
        sa.Column('prediction_factors', sa.JSON(), nullable=True),
        sa.Column('deadline', sa.Date(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tasks_status', 'tasks', ['status'])
    op.create_index('idx_tasks_project', 'tasks', ['project'])

    op.create_table(
        'sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=20), nullable=True),
        sa.Column('thread_id', sa.String(length=100), nullable=True),
        sa.Column('current_gate', sa.String(length=20), nullable=True),
        sa.Column('messages', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_sessions_task', 'sessions', ['task_id'])

    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=20), nullable=True),
        sa.Column('action', sa.String(length=50), nullable=False),
        sa.Column('actor', sa.String(length=50), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_audit_task', 'audit_log', ['task_id'])


def downgrade() -> None:
    op.drop_index('idx_audit_task', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('idx_sessions_task', table_name='sessions')
    op.drop_table('sessions')
    op.drop_index('idx_tasks_project', table_name='tasks')
    op.drop_index('idx_tasks_status', table_name='tasks')
    op.drop_table('tasks')
