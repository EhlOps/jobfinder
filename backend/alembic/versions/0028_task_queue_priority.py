"""tasks: move the priority lane's kinds to queue 'priority' (#124)

#124 gives the work someone is waiting on (profile extract, follow-up questions, audit, cover letter,
resume tailoring, the AI check, document parsing and link fetching) its own stream, `jobfinder:priority`,
read by worker-priority. Like 0026, this rewrites `queue` from the kind (only the moved kinds change), so
the column is the truth again for what reads it (the admin's per-queue metrics, `jobfinder tasks list`,
the previous relay on a downgrade). Only queued and running rows move: finished ones keep the
queue they actually waited in, so the metrics don't credit the new lane with waits behind match slices.
Queued rows also lose `sent_at`, so the relay sends them to the new stream at once rather than after
RELAY_RESEND. A message already on the old stream is harmless: claims are by id, so whichever delivery
claims the row first runs it, once, and the other is dropped as not claimable.

Rollout: run before the new workers start. A row the previous release's worker-ai or worker-io is
running when this lands still finishes there (claim and finish read the row by id, not by queue). Rows
the previous release still writes during the rollout name the old queue; the new relay routes by kind
(ledger.due_for_relay), so they reach worker-priority too.

Downgrade: moves the queued and running rows of these kinds back to the queues the previous release
reads (`ai` for the Claude kinds, `io` for parse_document and fetch_link; its relay reads only the
column), so its workers pick them up; queued ones lose `sent_at` again, for the relay to send them
there. Finished rows are left alone.

Revision ID: 0028
Revises: 0027
"""
import sqlalchemy as sa

from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None

# Frozen copy of tasks.kinds.QUEUES at this revision (kinds not listed are left alone).
QUEUES = {
    "priority": ("extract_profile", "followup_questions", "audit_profile", "cover_letter", "resume_tailor",
                 "ai_check", "parse_document", "fetch_link"),
    "ai": ("match_user", "collect_questions", "induce_recipe", "reinduce_recipe"),
    "io": ("ingest_tick", "ingest_board", "ingest_source", "plan_work", "daily_emails", "discover_boards",
           "score_coveted", "resolve_job_link", "audit_coverage"),
    "render": ("render_capture", "render_list"),
}
# Where the priority kinds were before (0026), for the downgrade.
PREVIOUS = {
    "ai": ("extract_profile", "followup_questions", "audit_profile", "cover_letter", "resume_tailor",
           "ai_check"),
    "io": ("parse_document", "fetch_link"),
}
LIVE = ("queued", "running")


def _move(kinds: tuple[str, ...], queue: str, statuses: tuple[str, ...]) -> None:
    tasks = sa.table("tasks", sa.column("kind", sa.String), sa.column("queue", sa.String),
                     sa.column("status", sa.String), sa.column("sent_at", sa.DateTime))
    op.execute(tasks.update().where(tasks.c.kind.in_(kinds), tasks.c.queue != queue,
                                    tasks.c.status.in_(statuses)).values(
        queue=queue, sent_at=sa.case((tasks.c.status == "queued", sa.null()), else_=tasks.c.sent_at)))


def upgrade() -> None:
    for queue, kinds in QUEUES.items():
        _move(kinds, queue, LIVE)


def downgrade() -> None:
    for queue, kinds in PREVIOUS.items():
        _move(kinds, queue, LIVE)
