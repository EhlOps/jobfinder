"""job_links: job URLs users paste; each resolves to a shared company board (origin 'user')

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa

from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'job_links',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('url', sa.String(length=2048), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('ats', sa.String(length=32), nullable=True),
        sa.Column('slug', sa.String(length=200), nullable=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id', ondelete='SET NULL'), nullable=True),
        sa.Column('error', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('user_id', 'url', name='uq_job_link_user_url'),
    )
    op.create_index('ix_job_links_user_created', 'job_links', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_job_links_user_created', table_name='job_links')
    op.drop_table('job_links')
