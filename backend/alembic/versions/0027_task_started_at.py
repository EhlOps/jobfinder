"""tasks: add `started_at`, when the latest attempt was claimed (#124)

PRD 1 of #124 ("every profile task returns within 2 minutes of queuing") needs the wait before a
worker picks a task up, apart from its run. `ledger.claim` stamps this on every successful claim, so
`started_at - run_after` is the latest attempt's wait and `finished_at - started_at` its run; the admin
schedule page's Queues table reports both per queue, over the rows a partial index on it finds.

Rollout: additive and nullable. Run before the new workers start; the previous release never reads or
writes it, and rows claimed before it stay NULL (left out of the metrics).

Downgrade: drops the index and the column (the recorded waits are lost; nothing else reads them).

Revision ID: 0027
Revises: 0026
"""
import sqlalchemy as sa

from alembic import op

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    # Empty when it's built (every row is NULL), so this takes no time.
    op.create_index("ix_tasks_started", "tasks", ["started_at"], postgresql_where=sa.text("started_at IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_tasks_started", table_name="tasks")
    op.drop_column("tasks", "started_at")
