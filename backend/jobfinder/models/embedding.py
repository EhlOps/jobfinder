"""Embedding retrieval (#126): vectors for postings and profile versions, plus the labels a future model
run learns from.

`job_embeddings` and `profile_embeddings` are derived: `jobfinder ops reindex` rebuilds them from `jobs`
and `profiles`, so the nightly dump leaves their data out. Every vector row names the model run that made
it (`model`, the name in that run's jobfinder_contract.json); vectors from different runs are never
compared, and a new run is indexed next to the old one before it is switched on.

`profile_versions` and `match_reviews` are not derived. They record what each profile version said and
how retrieval and Claude rated each scored job, which is the training and evaluation data for the next
model run (jobfinder-embedding-model, notebook 01). They stay in backups.
"""
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType

EMBED_DIM = 384  # bge-small; a model with another width needs a migration


class JobEmbedding(Base):
    """One retrieval vector per (posting, model run): the contract's `job_doc` text, no prompt."""

    __tablename__ = "job_embeddings"
    __table_args__ = (
        Index("ix_job_embeddings_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    # md5(title|company_name|description_text) when embedded (repositories.embeddings.SOURCE_HASH);
    # a mismatch means the posting changed and is embedded again.
    source_hash: Mapped[str] = mapped_column(String(32))
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfileEmbedding(Base):
    """A profile version's query vectors: one per target role plus the summary (`profile_queries`)."""

    __tablename__ = "profile_embeddings"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)  # position in profile_queries()
    text: Mapped[str] = mapped_column(Text)  # the query text as embedded (without the prompt)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfileVersion(Base):
    """What a profile said at one version, kept so evidence can be rebuilt as it was when it was scored."""

    __tablename__ = "profile_versions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[dict] = mapped_column(JSONType, default=dict)
    background: Mapped[dict] = mapped_column(JSONType, default=dict)
    facts: Mapped[list] = mapped_column(JSONType, default=list)  # [{question, answer}] answered at this version
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MatchReview(Base):
    """Append-only: one row per Claude score, with how retrieval had ranked that job (a training label)."""

    __tablename__ = "match_reviews"
    __table_args__ = (Index("ix_match_reviews_user_scored", "user_id", "scored_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_version: Mapped[int] = mapped_column(Integer)
    engine: Mapped[str] = mapped_column(String(16))  # prefilter|vector: what picked the job (MATCH_RETRIEVAL)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)  # embedding run, vector engine only
    retrieval_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-based, in the run's order
    retrieval_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # max cosine, vector engine
    prefilter_score: Mapped[float] = mapped_column(Float, default=0)
    llm_score: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(16))
    requirements: Mapped[list] = mapped_column(JSONType, default=list)  # Claude's [RequirementCheck]
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
