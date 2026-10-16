"""Admin › AI and Admin › Schedule: the Claude connection, the planner's schedule and per-board ingest health."""
from typing import Literal

from pydantic import BaseModel


class LastCall(BaseModel):
    ok: bool
    error_kind: str | None
    at: str


class AiStatus(BaseModel):
    configured: bool
    source: Literal["file", "env", "none"]
    last_call: LastCall | None


class TokenIn(BaseModel):
    token: str


class ActiveTask(BaseModel):
    kind: str
    status: str
    run_after: str


class ScheduleRow(BaseModel):
    user_id: int
    email: str
    last_seen_at: str | None
    last_run_at: str | None
    last_scored: int | None
    pending: int | None
    budget_left: int | None  # null = no daily limit
    last_planned_at: str | None
    next_due_at: str | None
    reason: str
    skip: str
    active: list[ActiveTask]


class TierCount(BaseModel):
    tier: int | None  # coveted tier; null = untiered
    boards: int
    due: int  # fetched on the next tick


class IngestCadence(BaseModel):
    adaptive: bool
    tick_minutes: int
    interval_hours: int  # the fixed cadence when not adaptive (and for jobspy/usajobs)
    tiers: list[TierCount]


class Schedule(BaseModel):
    backoff_until: str | None
    in_flight: int
    max_per_tick: int
    next_tick: str | None
    users: list[ScheduleRow]
    ingest: IngestCadence


class SourceRow(BaseModel):
    id: int
    name: str
    ats: str
    slug: str
    origin: str
    enabled: bool
    consecutive_failures: int
    last_success_at: str | None
    last_fetched_at: str | None
    last_error: str | None
    disabled_reason: str | None
    validated_at: str | None
    job_count: int
