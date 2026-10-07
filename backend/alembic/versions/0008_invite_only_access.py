"""invite-only access: nullable user columns, activated_at, rate_events

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa

from alembic import op

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

_NULLABLE = {
    'password_hash': sa.String(length=255),
    'timezone': sa.String(length=64),
    'digest_hour': sa.Integer(),
    'digest_enabled': sa.Boolean(),
    'is_admin': sa.Boolean(),
    'question_emails_enabled': sa.Boolean(),
}
_DEFAULTS = {
    'timezone': "'UTC'", 'digest_hour': '8', 'digest_enabled': 'true',
    'is_admin': 'false', 'question_emails_enabled': 'true',
}


def upgrade() -> None:
    for name, typ in _NULLABLE.items():
        op.alter_column('users', name, existing_type=typ, nullable=True, server_default=None)
    op.add_column('users', sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET activated_at = created_at")
    op.execute("UPDATE users SET email = lower(email) WHERE email <> lower(email)")
    op.create_check_constraint('ck_users_email_lower', 'users', 'email = lower(email)')
    op.create_table(
        'rate_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('key', sa.String(length=320), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_rate_events_kind_key_created', 'rate_events', ['kind', 'key', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_rate_events_kind_key_created', table_name='rate_events')
    op.drop_table('rate_events')
    op.drop_constraint('ck_users_email_lower', 'users', type_='check')
    op.execute("DELETE FROM users WHERE password_hash IS NULL")
    for name, default in _DEFAULTS.items():
        op.execute(f"UPDATE users SET {name} = {default} WHERE {name} IS NULL")
    for name, typ in _NULLABLE.items():
        op.alter_column('users', name, existing_type=typ, nullable=False,
                        server_default=sa.text(_DEFAULTS[name]) if name in ('is_admin', 'question_emails_enabled') else None)
    op.drop_column('users', 'activated_at')
