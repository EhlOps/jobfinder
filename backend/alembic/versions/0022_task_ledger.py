"""tasks: the ledger columns for valkey delivery (#48)

`queue` names the stream that delivers a task (tasks.kinds.QUEUES), backfilled from its kind;
`sent_at` is when the scheduler's relay last published it. `ix_tasks_due` serves the relay's scan of
due queued rows. Additive only: the legacy worker ignores both columns.

Revision ID: 0022
Revises: 0021
"""
import sqlalchemy as sa

from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

# Frozen copy of tasks.kinds.QUEUES at this revision (kinds not listed stay 'io').
AI_KINDS = (
    "extract_profile", "followup_questions", "audit_profile", "match_user", "collect_questions",
    "cover_letter", "resume_tailor", "ai_check", "daily_emails", "resolve_job_link", "audit_coverage",
    "reinduce_recipe",
)
RENDER_KINDS = ("render_capture", "render_list")


def upgrade() -> None:
    op.add_column("tasks", sa.Column("queue", sa.String(16), nullable=False, server_default="io"))
    op.add_column("tasks", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    tasks = sa.table("tasks", sa.column("kind", sa.String), sa.column("queue", sa.String))
    op.execute(tasks.update().where(tasks.c.kind.in_(AI_KINDS)).values(queue="ai"))
    op.execute(tasks.update().where(tasks.c.kind.in_(RENDER_KINDS)).values(queue="render"))
    op.create_index("ix_tasks_due", "tasks", ["queue", "run_after"], postgresql_where=sa.text("status = 'queued'"))


def downgrade() -> None:
    op.drop_index("ix_tasks_due", table_name="tasks")
    op.drop_column("tasks", "sent_at")
    op.drop_column("tasks", "queue")
