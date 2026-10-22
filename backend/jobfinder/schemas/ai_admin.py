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


class LaneStats(BaseModel):
    """One queue's tasks claimed in the last 24h (tasks.started_at): how long they waited to be claimed
    (started_at - run_after) and ran (finished_at - started_at), in seconds. #124's PRD 1 is measured here."""
    queue: str
    tasks: int
    wait_p50: float
    wait_p95: float
    run_p50: float | None  # null while none of them has finished
    run_p95: float | None
    slow_waits: int  # waits over ledger.SLOW_WAIT (60s)


class Schedule(BaseModel):
    backoff_until: str | None
    in_flight: int
    max_per_tick: int
    next_tick: str | None
    users: list[ScheduleRow]
    ingest: IngestCadence
    lanes: list[LaneStats]


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
