"""companies: per-source health tracking (consecutive_failures, last_success_at, disabled_reason)

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa

from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False))
    op.add_column('companies', sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('companies', sa.Column('disabled_reason', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'disabled_reason')
    op.drop_column('companies', 'last_success_at')
    op.drop_column('companies', 'consecutive_failures')
