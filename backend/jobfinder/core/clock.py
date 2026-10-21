"""Time. Code asks a clock for "now" instead of calling datetime.now(), so tests can fix the time.

A clock is any zero-argument callable returning an aware UTC datetime; `tests/fakes.py:FakeClock` is
one. `utcnow` is the real clock.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobfinder.core.config import Settings

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


# Interval schedules that start here fire on a fixed grid (anchor + k * interval), so a process that
# didn't start the schedule can still tell when it fires next.
GRID_ANCHOR = datetime(2000, 1, 1, tzinfo=UTC)


def next_on_grid(after: datetime, every: timedelta, anchor: datetime = GRID_ANCHOR) -> datetime:
    """The first anchor + k * every at or after `after`: rounded up the way APScheduler's interval
    trigger picks its next fire time from its start_date (which also turns a zero interval into 1s)."""
    every = max(every, timedelta(seconds=1))
    return anchor - ((anchor - after) // every) * every


def planner_interval(s: Settings) -> timedelta:
    """How often the plan_work cron fires, on the grid (scheduler/cron.py); the admin schedule page
    shows its next tick from the same definition."""
    return timedelta(minutes=s.planner_interval_minutes)
