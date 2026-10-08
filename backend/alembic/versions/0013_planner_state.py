"""background planner state and last_seen_at

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa

from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET last_seen_at = activated_at WHERE activated_at IS NOT NULL")
    op.create_table(
        'planner_state',
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('last_planned_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_due_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_reason', sa.String(200), server_default='', nullable=False),
        sa.Column('last_skip', sa.String(200), server_default='', nullable=False),
    )


def downgrade() -> None:
    op.drop_table('planner_state')
    op.drop_column('users', 'last_seen_at')
