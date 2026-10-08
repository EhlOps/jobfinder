"""profiles.career_stage: lets matching notice when a graduation date passes

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa

from alembic import op

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('profiles', sa.Column('career_stage', sa.String(16), server_default='', nullable=False))


def downgrade() -> None:
    op.drop_column('profiles', 'career_stage')
