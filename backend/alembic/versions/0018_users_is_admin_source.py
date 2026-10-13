"""users.is_admin becomes the source of truth for admin (ADMIN_EMAILS is retired)

Revision ID: 0018
Revises: 0017
"""
import os

import sqlalchemy as sa

from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One-time carry-over: anyone listed in ADMIN_EMAILS keeps admin. The flag only mirrored the env until now.
    emails = [e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]
    if emails:
        op.get_bind().execute(
            sa.text("UPDATE users SET is_admin = (lower(email) = ANY(:emails))").bindparams(
                sa.bindparam("emails", value=emails, type_=sa.ARRAY(sa.String()))
            )
        )
    op.execute("UPDATE users SET is_admin = false WHERE is_admin IS NULL")
    op.alter_column('users', 'is_admin', existing_type=sa.Boolean(), nullable=False, server_default=sa.text('false'))


def downgrade() -> None:
    op.alter_column('users', 'is_admin', existing_type=sa.Boolean(), nullable=True, server_default=None)
