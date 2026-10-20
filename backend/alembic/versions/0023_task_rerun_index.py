"""tasks: one queued twin per dedupe_key, running twins allowed; relay index by (run_after, id) (#48)

`uq_tasks_dedupe_active` (queued or running) becomes `uq_tasks_dedupe_queued` (queued only), so a rerun
asked for while its twin runs can queue behind it (tasks.ledger.add(rerun=True)). `ix_tasks_due` led
with `queue`, which the relay's scan doesn't filter on; it now matches its (run_after, id) order.

Rollout: old code's dedupe insert (ON CONFLICT ... WHERE status IN ('queued','running')) can't infer
the narrower index, so it errors on pods still running the old image until they have rolled (the
migrate Job runs first). Their planner work comes back on the next tick.

Downgrade: the wider index can't hold a queued rerun next to its running twin, so those reruns are
failed first (their twin is running the same work).

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa

from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_tasks_dedupe_queued", "tasks", ["dedupe_key"], unique=True,
                    postgresql_where=sa.text("status = 'queued'"))
    op.drop_index("uq_tasks_dedupe_active", table_name="tasks")
    op.drop_index("ix_tasks_due", table_name="tasks")
    op.create_index("ix_tasks_due", "tasks", ["run_after", "id"], postgresql_where=sa.text("status = 'queued'"))


def downgrade() -> None:
    op.execute(
        "UPDATE tasks q SET status = 'failed', finished_at = now(),"
        " error = 'dropped by migration 0023 downgrade: its twin was running'"
        " WHERE q.status = 'queued' AND EXISTS (SELECT 1 FROM tasks r"
        "  WHERE r.dedupe_key = q.dedupe_key AND r.id <> q.id AND r.status = 'running')"
    )
    op.drop_index("ix_tasks_due", table_name="tasks")
    op.create_index("ix_tasks_due", "tasks", ["queue", "run_after"], postgresql_where=sa.text("status = 'queued'"))
    op.create_index("uq_tasks_dedupe_active", "tasks", ["dedupe_key"], unique=True,
                    postgresql_where=sa.text("status IN ('queued','running')"))
    op.drop_index("uq_tasks_dedupe_queued", table_name="tasks")
