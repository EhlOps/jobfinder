"""requirement-level match checks and the recruiter profile audit

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("job_matches", sa.Column("requirements", postgresql.JSONB(), server_default="[]", nullable=False))
    op.add_column("job_matches", sa.Column("hire_verdict", sa.String(8), server_default="", nullable=False))
    op.add_column("job_matches", sa.Column("recruiter_take", sa.Text(), server_default="", nullable=False))
    op.add_column("profiles", sa.Column("dossier", postgresql.JSONB(), server_default="{}", nullable=False))
    op.add_column("profiles", sa.Column("readiness", sa.Integer(), server_default="0", nullable=False))
    op.add_column("profiles", sa.Column("audit", postgresql.JSONB(), server_default="{}", nullable=False))
    # Existing scores came from the old holistic prompt: rescore everything under the new rubric.
    op.execute("UPDATE profiles SET version = version + 1")


def downgrade() -> None:
    for t, cols in (("job_matches", ["requirements", "hire_verdict", "recruiter_take"]), ("profiles", ["dossier", "readiness", "audit"])):
        for c in cols:
            op.drop_column(t, c)
