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
    # 1. Create tasks table
    op.create_table(
        'tasks',
        sa.Column('id', sa.String(length=20), nullable=False),
        sa.Column('project', sa.String(length=50), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('raw_input', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='todo'),
        sa.Column('current_gate', sa.String(length=20), nullable=False, server_default='spec'),
        sa.Column('mode', sa.String(length=20), nullable=False, server_default='supervised'),
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
        sa.Column('awaiting_approval', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('approval_prompt', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('deadline', sa.Date(), nullable=True),
        sa.Column('session_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint('executor IS NULL OR reviewer IS NULL OR executor <> reviewer', name='ck_tasks_four_eyes'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_tasks_status', 'tasks', ['status'])
    op.create_index('idx_tasks_project', 'tasks', ['project'])
    op.create_index('idx_tasks_gate', 'tasks', ['current_gate'])
    op.create_index('idx_tasks_session', 'tasks', ['session_id'])

    # 2. Create gate_records table
    op.create_table(
        'gate_records',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('task_id', sa.String(length=20), nullable=False),
        sa.Column('gate_type', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('executor', sa.String(length=50), nullable=True),
        sa.Column('reviewer', sa.String(length=50), nullable=True),
        sa.Column('input_payload', sa.JSON(), nullable=True),
        sa.Column('output_payload', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.CheckConstraint('executor IS NULL OR reviewer IS NULL OR executor <> reviewer', name='ck_gate_records_four_eyes'),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_gate_records_task', 'gate_records', ['task_id'])
    op.create_index('idx_gate_records_type', 'gate_records', ['gate_type'])

    # 3. Create sessions table
    op.create_table(
        'sessions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=20), nullable=True),
        sa.Column('thread_id', sa.String(length=100), nullable=True),
        sa.Column('current_gate', sa.String(length=20), nullable=True),
        sa.Column('checkpoint_id', sa.String(length=100), nullable=True),
        sa.Column('state_payload', sa.JSON(), nullable=True),
        sa.Column('messages', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_sessions_task', 'sessions', ['task_id'])
    op.create_index('idx_sessions_thread', 'sessions', ['thread_id'])
    op.create_index('idx_sessions_checkpoint', 'sessions', ['checkpoint_id'])

    # 4. Create audit_log table
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
    op.drop_index('idx_audit_task', table_name='audit_log', if_exists=True)
    op.drop_table('audit_log', if_exists=True)
    op.drop_index('idx_sessions_checkpoint', table_name='sessions', if_exists=True)
    op.drop_index('idx_sessions_thread', table_name='sessions', if_exists=True)
    op.drop_index('idx_sessions_task', table_name='sessions', if_exists=True)
    op.drop_table('sessions', if_exists=True)
    op.drop_index('idx_gate_records_type', table_name='gate_records', if_exists=True)
    op.drop_index('idx_gate_records_task', table_name='gate_records', if_exists=True)
    op.drop_table('gate_records', if_exists=True)
    op.drop_index('idx_tasks_session', table_name='tasks', if_exists=True)
    op.drop_index('idx_tasks_gate', table_name='tasks', if_exists=True)
    op.drop_index('idx_tasks_project', table_name='tasks', if_exists=True)
    op.drop_index('idx_tasks_status', table_name='tasks', if_exists=True)
    op.drop_table('tasks', if_exists=True)
