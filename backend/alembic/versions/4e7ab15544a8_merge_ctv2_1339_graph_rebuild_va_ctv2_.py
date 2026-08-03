"""merge CTV2-1339 graph rebuild va CTV2-1341 spec core

Revision ID: 4e7ab15544a8
Revises: ('046_living_spec_core', '046_outbox_events_graph_rebuild')
Create Date: 2026-08-04 03:25:18.065096

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e7ab15544a8'
down_revision: Union[str, None] = ('046_living_spec_core', '046_outbox_events_graph_rebuild')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
