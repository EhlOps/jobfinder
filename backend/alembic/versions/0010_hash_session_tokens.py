"""store sha256 of session tokens instead of the tokens

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing cookies keep working: the app now hashes the cookie value before looking it up.
    op.execute("UPDATE sessions SET token = encode(sha256(convert_to(token, 'UTF8')), 'hex')")
    op.execute("DELETE FROM sessions WHERE expires_at < now()")


def downgrade() -> None:
    # Hashes can't be turned back into tokens: everyone signs in again.
    op.execute("DELETE FROM sessions")
