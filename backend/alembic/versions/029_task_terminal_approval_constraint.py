"""Prevent terminal tasks from remaining in an approval state (CTV2-206).

Revision ID: 029_task_terminal_approval
Revises: 028_dispatch_decisions
Create Date: 2026-07-29 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "029_task_terminal_approval"
down_revision: Union[str, None] = "028_dispatch_decisions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    checks = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("tasks")
        if constraint.get("name")
    }
    if "ck_tasks_terminal_not_awaiting_approval" not in checks:
        with op.batch_alter_table("tasks") as batch:
            batch.create_check_constraint(
                "ck_tasks_terminal_not_awaiting_approval",
                "status NOT IN ('done', 'changes-requested') "
                "OR awaiting_approval IS NOT TRUE",
            )


def downgrade() -> None:
    checks = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints("tasks")
        if constraint.get("name")
    }
    if "ck_tasks_terminal_not_awaiting_approval" in checks:
        with op.batch_alter_table("tasks") as batch:
            batch.drop_constraint(
                "ck_tasks_terminal_not_awaiting_approval", type_="check"
            )
