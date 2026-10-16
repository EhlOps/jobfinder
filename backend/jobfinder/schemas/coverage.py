"""Admin › Coverage: coveted employers, the ones with no board yet, and pinning them."""
from typing import Literal

from pydantic import BaseModel, Field


class CoverageRow(BaseModel):
    company_id: int
    name: str
    ats: str
    slug: str
    tier: int
    score: float
    pinned: bool | None
    board_status: Literal["ok", "failing", "disabled", "none"]
    gap: bool
    job_count: int
    last_success_at: str | None
    recipe_state: str | None
    last_error: str | None  # the board's own fetch error or why it was switched off
    resolve_error: str | None  # the coverage audit's last attempt
    last_resolved_at: str | None
    domain: str | None
    careers_url: str | None


class TargetRow(BaseModel):
    id: int
    name: str
    domain: str
    source: str
    score: float
    last_resolved_at: str | None
    last_error: str | None
    careers_url: str


class Coverage(BaseModel):
    companies: list[CoverageRow]
    targets: list[TargetRow]  # employers with no board yet


class PinIn(BaseModel):
    company_id: int | None = None
    name: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=255)


class UnpinIn(BaseModel):
    company_id: int | None = None
    target_id: int | None = None
    exclude: bool = False  # True: mark "not coveted" (tier 3); False: back to the computed score


class PinOut(BaseModel):
    company_id: int | None = None
    target_id: int | None = None
    task_id: int | None = None
