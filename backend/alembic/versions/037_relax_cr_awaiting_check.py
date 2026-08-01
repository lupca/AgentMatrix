"""changes-requested may await a re-dispatch gate (CTV2-234).

The old check treated changes-requested as terminal and forbade
awaiting_approval there — which made a supervised replan round impossible:
requesting a re-dispatch sets awaiting_approval while the status is still
changes-requested. Only 'done' keeps the invariant.

Revision ID: 037_relax_cr_awaiting_check
Revises: 036_add_agent_legacy_profile
Create Date: 2026-08-01 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "037_relax_cr_awaiting_check"
down_revision: Union[str, None] = "036_add_agent_legacy_profile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAME = "ck_tasks_terminal_not_awaiting_approval"


def upgrade() -> None:
    # batch mode: SQLite (used by the migration-cycle tests) cannot ALTER
    # constraints in place — batch rebuilds the table there, while Postgres
    # gets a plain DROP/ADD CONSTRAINT.
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(
            NAME, "status <> 'done' OR awaiting_approval IS NOT TRUE"
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(
            NAME,
            "status NOT IN ('done', 'changes-requested') OR awaiting_approval IS NOT TRUE",
        )
