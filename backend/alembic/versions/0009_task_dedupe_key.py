"""task dedupe key: atomic enqueue dedupe

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa

from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('dedupe_key', sa.String(length=64), nullable=True))
    op.create_index(
        'uq_tasks_dedupe_active', 'tasks', ['dedupe_key'], unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )


def downgrade() -> None:
    op.drop_index('uq_tasks_dedupe_active', table_name='tasks')
    op.drop_column('tasks', 'dedupe_key')
