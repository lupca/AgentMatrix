"""Add cache_write_tokens and usage_is_measured to llm_usage

CTV2-1424: split the four token quantities (fresh / cache_read /
cache_write / output) so the token brake can exclude cache reads.

input_tokens keeps its existing contract (total = fresh + cache_read) so
existing reports are not broken.  cache_write_tokens is a new column for
the cache-creation (write) portion.  usage_is_measured distinguishes
vendor-reported telemetry from estimates.

Revision ID: 059_llm_usage_cache_write
Revises: 058_open_gates_view
Create Date: 2026-08-07 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "059_llm_usage_cache_write"
down_revision: Union[str, None] = "058_open_gates_view"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cache_write_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        batch_op.add_column(
            sa.Column(
                "usage_is_measured",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        batch_op.create_check_constraint(
            "ck_llm_usage_cache_write_nonnegative",
            "cache_write_tokens >= 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch_op:
        batch_op.drop_constraint("ck_llm_usage_cache_write_nonnegative", type_="check")
        batch_op.drop_column("usage_is_measured")
        batch_op.drop_column("cache_write_tokens")
