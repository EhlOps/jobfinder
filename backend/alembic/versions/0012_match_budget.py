"""per-user match scoring budget

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa

from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('match_budget_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('users', sa.Column('match_budget', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'match_budget')
    op.drop_column('users', 'match_budget_enabled')
