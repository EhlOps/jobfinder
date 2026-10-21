"""Employers: the ATS boards we poll and coveted targets we may not have a board for yet."""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from jobfinder.core.db import Base
from jobfinder.models.base import JSONType


class Company(Base):
    """A company whose public ATS board we poll. The table is the only list of boards (seeded by migration 0021)."""

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats", "slug", name="uq_company_ats_slug"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    ats: Mapped[str] = mapped_column(String(32))  # see integrations.ats.registry.ATS_FETCHERS
    slug: Mapped[str] = mapped_column(String(200))
    prestige_tier: Mapped[int] = mapped_column(Integer, default=3)  # 1..5
    size: Mapped[str] = mapped_column(String(16), default="mid")  # startup|mid|large
    industry: Mapped[str] = mapped_column(String(100), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    sponsors_visas: Mapped[bool | None] = mapped_column(nullable=True)  # None = unknown
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default="seed", server_default="seed")  # seed|discovered|user
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ats='recipe' boards: how to read the site (integrations.recipes.Recipe), its lifecycle and yield history
    recipe: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    recipe_state: Mapped[str | None] = mapped_column(String(16), nullable=True)  # induced|validated|active|degraded|disabled
    recipe_health: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # how sought-after the employer is (services.coverage); tier 1 is the most
    coveted_score: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    coveted_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coveted_pinned: Mapped[bool | None] = mapped_column(nullable=True)  # admin override: True pin, False unpin
    # adaptive cadence: per-board interval and when it's next due (None = due now)
    fetch_interval_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_fetch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CovetedTarget(Base):
    """A sought-after employer we may not have a board for yet (from outside lists or admin pins)."""

    __tablename__ = "coveted_targets"
    __table_args__ = (UniqueConstraint("domain", name="uq_coveted_target_domain"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(255))  # registrable domain, e.g. acme.com
    source: Mapped[str] = mapped_column(String(64), default="admin")  # admin|github_lists|...
    score: Mapped[float] = mapped_column(Float, default=0, server_default="0")
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    last_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
