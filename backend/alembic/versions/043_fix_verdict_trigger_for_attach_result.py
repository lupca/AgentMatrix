"""fix verdict trigger to accept attach_result gate type

Revision ID: 043_fix_verdict_trigger
Revises: 042_agent_notes
Create Date: 2026-08-02
"""
from alembic import op
from typing import Sequence, Union

revision: str = "043_fix_verdict_trigger"
down_revision: Union[str, None] = "042_agent_notes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.ct_enforce_done_verdict()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'done' AND NOT EXISTS (
                SELECT 1
                FROM gate_records gr
                WHERE gr.task_id = NEW.id
                  AND gr.gate_type IN ('verdict', 'attach_result')
                  AND gr.status = 'approved'
                  AND (
                    gr.output_ref = 'pass'
                    OR gr.output_payload ->> 'verdict' = 'pass'
                    OR gr.output_payload ->> 'option' = 'done'
                  )
            ) THEN
                RAISE EXCEPTION
                    'task % cannot be done without an approved passing verdict',
                    NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION public.ct_enforce_done_verdict()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status = 'done' AND NOT EXISTS (
                SELECT 1
                FROM gate_records gr
                WHERE gr.task_id = NEW.id
                  AND gr.gate_type = 'verdict'
                  AND gr.status = 'approved'
                  AND (
                    gr.output_ref = 'pass'
                    OR gr.output_payload ->> 'verdict' = 'pass'
                  )
            ) THEN
                RAISE EXCEPTION
                    'task % cannot be done without an approved passing verdict',
                    NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)
