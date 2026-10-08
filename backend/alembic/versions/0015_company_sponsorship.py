"""companies.sponsors_visas: per-company visa sponsorship flag (null = unknown)

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa

from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('sponsors_visas', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'sponsors_visas')
