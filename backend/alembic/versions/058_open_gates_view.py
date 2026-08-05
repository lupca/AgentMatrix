"""Create open_gates view -- the gates that are ACTUALLY undecided.

Both gate ledgers are append-only: a decision is a CHILD row pointing at the
pending row via `parent_id`, and the pending root keeps `status='pending'`
forever.  So the obvious query -- `WHERE status='pending'` -- is wrong, and
wrong in the direction that costs the most: it answers "what is blocking me"
with a pile of gates that were decided months ago.

Measured on the live DB the day this view was written:

| query                                      | task gates | admin gates |
|--------------------------------------------|-----------:|------------:|
| `WHERE status='pending'`                   |        650 |          94 |
| ... and no decision child (this view)      |         25 |           0 |
| ... and the task is still live (`NOT moot`)|          8 |           0 |

98% noise for task gates, 100% for admin gates.  Two coordinators lost a day
to exactly this.  The knowledge cannot live in a doc nobody opens at the
moment they write the SQL -- it has to live where the SQL is written, so it
ships as a view plus two lines in the `query_db` schema summary.

`gate_record_id` is text, and admin gates carry the `admin:<id>` form, because
that is literally what `approve_gate` takes -- the answer to "which gate" is
also the argument that clears it, with no translation step in between.

Revision ID: 058_open_gates_view
Revises: 057_task_events_task_id_nullable
Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa

revision = "058_open_gates_view"
down_revision = "057_task_events_task_id_nullable"
branch_labels = None
depends_on = None


OPEN_GATES_SQL = """
CREATE OR REPLACE VIEW open_gates AS
SELECT
    'task'::text                       AS scope,
    g.id::text                         AS gate_record_id,
    g.task_id,
    g.gate_type,
    g.status,
    g.actor,
    g.mode,
    g.executor,
    g.reviewer,
    g.created_at,
    t.project,
    t.status                           AS task_status,
    (t.archived_at IS NOT NULL
     OR t.status IN ('done', 'cancelled')) AS moot
FROM gate_records g
JOIN tasks t ON t.id = g.task_id
WHERE g.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM gate_records c WHERE c.parent_id = g.id
  )
UNION ALL
SELECT
    'admin'::text                      AS scope,
    'admin:' || a.id::text             AS gate_record_id,
    NULL::character varying(20)        AS task_id,
    a.entity || '/' || a.action        AS gate_type,
    a.status,
    a.actor,
    a.mode,
    NULL::character varying(50)        AS executor,
    NULL::character varying(50)        AS reviewer,
    a.created_at,
    NULL::character varying(50)        AS project,
    NULL::character varying(20)        AS task_status,
    false                              AS moot
FROM admin_gate_records a
WHERE a.status = 'pending'
  AND NOT EXISTS (
      SELECT 1 FROM admin_gate_records c WHERE c.parent_id = a.id
  )
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text(OPEN_GATES_SQL))
    # query_db runs as ct_readonly_user; without the grant the view exists but
    # is invisible to the one caller it was built for (same trap as 039/042).
    bind.execute(
        sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'ct_readonly_user') THEN
                    GRANT SELECT ON open_gates TO ct_readonly_user;
                END IF;
            END
            $$;
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text("DROP VIEW IF EXISTS open_gates"))
