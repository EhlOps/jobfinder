"""adaptive scraping foundation: recipe boards, coveted score, per-board cadence, coveted_targets, job_links.rung/job_id

Revision ID: 0020
Revises: 0019
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('recipe', postgresql.JSONB(), nullable=True))
    op.add_column('companies', sa.Column('recipe_state', sa.String(length=16), nullable=True))
    op.add_column('companies', sa.Column('recipe_health', postgresql.JSONB(), nullable=True))
    op.add_column('companies', sa.Column('coveted_score', sa.Float(), server_default='0', nullable=False))
    op.add_column('companies', sa.Column('coveted_tier', sa.Integer(), nullable=True))
    op.add_column('companies', sa.Column('coveted_pinned', sa.Boolean(), nullable=True))
    op.add_column('companies', sa.Column('fetch_interval_minutes', sa.Integer(), nullable=True))
    op.add_column('companies', sa.Column('next_fetch_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'coveted_targets',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=False),
        sa.Column('score', sa.Float(), server_default='0', nullable=False),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='SET NULL'), nullable=True),
        sa.Column('last_resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('domain', name='uq_coveted_target_domain'),
    )
    op.add_column('job_links', sa.Column('rung', sa.String(length=32), nullable=True))
    op.add_column('job_links', sa.Column('job_id', sa.Integer(), sa.ForeignKey('jobs.id', ondelete='SET NULL'),
                                         nullable=True))


def downgrade() -> None:
    op.drop_column('job_links', 'job_id')
    op.drop_column('job_links', 'rung')
    op.drop_table('coveted_targets')
    for col in ('next_fetch_at', 'fetch_interval_minutes', 'coveted_pinned', 'coveted_tier', 'coveted_score',
                'recipe_health', 'recipe_state', 'recipe'):
        op.drop_column('companies', col)
