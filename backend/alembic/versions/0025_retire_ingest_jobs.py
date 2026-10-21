"""tasks: fail the queued and running ingest_jobs rows, a kind that no longer exists (#53)

ingest_jobs was the whole-run ingest, kept as an alias of ingest_tick so rows queued before the switch
still ran. #53 removes the alias, so a row still queued or running would reach a worker that has no
handler for it. This fails those rows (the scheduler's next ingest_tick does the same work) and they
are pruned with the other finished rows after TASK_PRUNE_DAYS.

Rollout: run before the new workers start. Old code would have run the rows as a tick; skipping them
loses nothing but one ingest run.

Downgrade: a no-op. Failed rows stay failed.

Revision ID: 0025
Revises: 0024
"""
from alembic import op

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE tasks SET status = 'failed', finished_at = now(),"
        " error = 'ingest_jobs was removed (#53); ingest_tick replaces it'"
        " WHERE kind = 'ingest_jobs' AND status IN ('queued', 'running')"
    )


def downgrade() -> None:
    pass
