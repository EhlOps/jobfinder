"""tasks: partial index on finished_at for done/failed rows, for maintenance's prune (#50)

Maintenance deletes done and failed rows that finished more than TASK_PRUNE_DAYS ago, at most 5000 per
tick (tasks.ledger.prune). `ix_tasks_finished` lets it find them, oldest first, without scanning the
queued and running rows.

Rollout: additive. Old code ignores the index; new code's prune works without it, only slower, so the
order of migrate and rollout doesn't matter. The first prune after deploy clears the backlog over a few
ticks.

Downgrade: drops the index. Rows already pruned stay deleted.

Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa

from alembic import op

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_finished", "tasks", ["finished_at"],
                    postgresql_where=sa.text("status IN ('done', 'failed')"))


def downgrade() -> None:
    op.drop_index("ix_tasks_finished", table_name="tasks")
