"""Consolidate task transitions around an immutable gate ledger.

Revision ID: 007_gate_system
Revises: 006_llm_usage
Create Date: 2026-07-26 22:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "007_gate_system"
down_revision: Union[str, None] = "006_llm_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    with op.batch_alter_table("gate_records") as batch:
        batch.add_column(sa.Column("actor", sa.String(50), nullable=True))
        batch.add_column(sa.Column("mode", sa.String(20), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(100), nullable=True))
        batch.add_column(sa.Column("input_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("output_ref", sa.String(255), nullable=True))
        batch.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))

    records = bind.execute(sa.text("SELECT id FROM gate_records")).fetchall()
    for (record_id,) in records:
        bind.execute(
            sa.text(
                "UPDATE gate_records SET "
                "actor = 'system:migration', mode = 'supervised', "
                "idempotency_key = :idempotency_key, input_hash = :input_hash "
                "WHERE id = :record_id"
            ),
            {
                "record_id": record_id,
                "idempotency_key": f"legacy-gate-{record_id}",
                "input_hash": f"{record_id:064x}"[-64:],
            },
        )
    bind.execute(
        sa.text(
            "UPDATE gate_records SET status = CASE "
            "WHEN status = 'pending' THEN 'pending' "
            "WHEN status IN ('rejected', 'failed', 'error') THEN 'rejected' "
            "ELSE 'approved' END"
        )
    )

    gate_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("gate_records")
        if constraint.get("name")
    }
    with op.batch_alter_table("gate_records") as batch:
        if "ck_gate_records_four_eyes" in gate_checks:
            batch.drop_constraint("ck_gate_records_four_eyes", type_="check")
        batch.alter_column("actor", existing_type=sa.String(50), nullable=False)
        batch.alter_column("mode", existing_type=sa.String(20), nullable=False)
        batch.alter_column(
            "idempotency_key", existing_type=sa.String(100), nullable=False
        )
        batch.alter_column("input_hash", existing_type=sa.String(64), nullable=False)
        batch.create_foreign_key(
            "fk_gate_records_parent",
            "gate_records",
            ["parent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_gate_records_status",
            "status IN ('pending', 'approved', 'rejected')",
        )
        batch.create_check_constraint(
            "ck_gate_records_mode",
            "mode IN ('supervised', 'plan-only', 'bypass')",
        )
        batch.create_check_constraint(
            "ck_gate_records_four_eyes",
            "executor IS NULL OR reviewer IS NULL "
            "OR lower(trim(executor)) <> lower(trim(reviewer))",
        )
        batch.create_unique_constraint(
            "uq_gate_records_task_idempotency",
            ["task_id", "idempotency_key"],
        )
    op.create_index("idx_gate_records_parent", "gate_records", ["parent_id"])

    # Existing rows that cannot prove independent acceptance must not remain done.
    bind.execute(
        sa.text(
            "UPDATE tasks SET status = 'awaiting-review', "
            "current_gate = 'review_order', completed_at = NULL "
            "WHERE status = 'done'"
        )
    )
    task_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("tasks")
        if constraint.get("name")
    }
    with op.batch_alter_table("tasks") as batch:
        if "ck_tasks_four_eyes" in task_checks:
            batch.drop_constraint("ck_tasks_four_eyes", type_="check")
        batch.create_check_constraint(
            "ck_tasks_four_eyes",
            "executor IS NULL OR reviewer IS NULL "
            "OR lower(trim(executor)) <> lower(trim(reviewer))",
        )
        batch.create_check_constraint(
            "ck_tasks_done_invariants",
            "status <> 'done' OR ("
            "executor IS NOT NULL AND reviewer IS NOT NULL "
            "AND lower(trim(executor)) <> lower(trim(reviewer)) "
            "AND result_ref IS NOT NULL AND trim(result_ref) <> ''"
            ")",
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION ct_gate_records_immutable() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'gate_records is an append-only ledger';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_gate_records_immutable
            BEFORE UPDATE OR DELETE ON gate_records
            FOR EACH ROW EXECUTE FUNCTION ct_gate_records_immutable()
            """
        )
        op.execute(
            """
            CREATE FUNCTION ct_enforce_done_verdict() RETURNS trigger AS $$
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
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE CONSTRAINT TRIGGER trg_tasks_done_verdict
            AFTER INSERT OR UPDATE ON tasks
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION ct_enforce_done_verdict()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_done_verdict ON tasks")
        op.execute("DROP FUNCTION IF EXISTS ct_enforce_done_verdict()")
        op.execute("DROP TRIGGER IF EXISTS trg_gate_records_immutable ON gate_records")
        op.execute("DROP FUNCTION IF EXISTS ct_gate_records_immutable()")

    task_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("tasks")
        if constraint.get("name")
    }
    with op.batch_alter_table("tasks") as batch:
        if "ck_tasks_done_invariants" in task_checks:
            batch.drop_constraint("ck_tasks_done_invariants", type_="check")
        if "ck_tasks_four_eyes" in task_checks:
            batch.drop_constraint("ck_tasks_four_eyes", type_="check")
        batch.create_check_constraint(
            "ck_tasks_four_eyes",
            "executor IS NULL OR reviewer IS NULL OR executor <> reviewer",
        )

    op.drop_index("idx_gate_records_parent", table_name="gate_records")
    gate_checks = {
        constraint["name"]
        for constraint in sa.inspect(bind).get_check_constraints("gate_records")
        if constraint.get("name")
    }
    with op.batch_alter_table("gate_records") as batch:
        batch.drop_constraint(
            "uq_gate_records_task_idempotency", type_="unique"
        )
        batch.drop_constraint("fk_gate_records_parent", type_="foreignkey")
        for name in (
            "ck_gate_records_status",
            "ck_gate_records_mode",
            "ck_gate_records_four_eyes",
        ):
            if name in gate_checks:
                batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(
            "ck_gate_records_four_eyes",
            "executor IS NULL OR reviewer IS NULL OR executor <> reviewer",
        )
        batch.drop_column("parent_id")
        batch.drop_column("output_ref")
        batch.drop_column("input_hash")
        batch.drop_column("idempotency_key")
        batch.drop_column("mode")
        batch.drop_column("actor")
