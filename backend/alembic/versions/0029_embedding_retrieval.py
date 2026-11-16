"""embedding retrieval: pgvector, job and profile vectors, label tables (#126)

#126 picks each profile's new jobs by nearest-neighbour search over embeddings (bge-small-jobfinder-v2,
384-d) instead of a regex scan of every unmatched job. This adds the pgvector extension and four tables:
- `job_embeddings`: one vector per (job, model run), with an HNSW cosine index. Derived.
- `profile_embeddings`: a profile version's query vectors, per model run. Derived.
- `profile_versions`: what each profile version said (status, background, answered facts). Labels.
- `match_reviews`: one append-only row per Claude score, with how retrieval ranked that job. Labels.
The derived tables are rebuilt by `jobfinder ops reindex` and left out of the nightly dump's data.

Rollout: needs the pgvector Postgres image (deploy/k8s/base/postgres) running before this migrates;
`create extension` fails on plain postgres:16. The previous release ignores the new tables.

Downgrade: drops the four tables and the extension. Only derived vectors and labels are lost.

Revision ID: 0029
Revises: 0028
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None

DIM = 384


def upgrade() -> None:
    op.execute("create extension if not exists vector")
    op.create_table(
        "job_embeddings",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("source_hash", sa.String(32), nullable=False),
        sa.Column("embedding", Vector(DIM), nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_job_embeddings_hnsw", "job_embeddings", ["embedding"], postgresql_using="hnsw",
                    postgresql_with={"m": 16, "ef_construction": 64},
                    postgresql_ops={"embedding": "vector_cosine_ops"})
    op.create_table(
        "profile_embeddings",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("profile_version", sa.Integer(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(DIM), nullable=False),
        sa.Column("embedded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "profile_versions",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("status", postgresql.JSONB(), nullable=False),
        sa.Column("background", postgresql.JSONB(), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "match_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("retrieval_rank", sa.Integer(), nullable=True),
        sa.Column("retrieval_score", sa.Float(), nullable=True),
        sa.Column("prefilter_score", sa.Float(), nullable=False),
        sa.Column("llm_score", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_match_reviews_user_scored", "match_reviews", ["user_id", "scored_at"])


def downgrade() -> None:
    op.drop_index("ix_match_reviews_user_scored", table_name="match_reviews")
    op.drop_table("match_reviews")
    op.drop_table("profile_versions")
    op.drop_table("profile_embeddings")
    op.drop_index("ix_job_embeddings_hnsw", table_name="job_embeddings")
    op.drop_table("job_embeddings")
    op.execute("drop extension if exists vector")
