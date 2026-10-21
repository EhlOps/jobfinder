"""tasks: rewrite `queue` for the kinds that moved queues in #50

0022 backfilled daily_emails, resolve_job_link and audit_coverage as 'ai'; #50 moved them to 'io'. The
relay and the after-commit publish routed by kind to cover the stale column. This rewrites every row's
queue from its kind, so the column is the truth again (ledger.requeue, retries and the reaper keep it so).

Rollout: run before the new relay starts; it reads the column only. The previous release routes by kind
and is unaffected.

Downgrade: a no-op. The rewritten values are the ones the previous release routes to anyway.

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa

from alembic import op

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None

# Frozen copy of tasks.kinds.QUEUES at this revision (kinds not listed are left alone).
QUEUES = {
    "ai": ("extract_profile", "followup_questions", "audit_profile", "match_user", "collect_questions",
           "cover_letter", "resume_tailor", "ai_check", "induce_recipe", "reinduce_recipe"),
    "io": ("ingest_tick", "ingest_board", "ingest_source", "plan_work", "daily_emails", "discover_boards",
           "score_coveted", "resolve_job_link", "audit_coverage", "parse_document", "fetch_link"),
    "render": ("render_capture", "render_list"),
}


def upgrade() -> None:
    tasks = sa.table("tasks", sa.column("kind", sa.String), sa.column("queue", sa.String))
    for queue, kinds in QUEUES.items():
        op.execute(tasks.update().where(tasks.c.kind.in_(kinds), tasks.c.queue != queue).values(queue=queue))


def downgrade() -> None:
    pass
