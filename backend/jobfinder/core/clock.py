"""Time. Code asks a clock for "now" instead of calling datetime.now(), so tests can fix the time.

A clock is any zero-argument callable returning an aware UTC datetime; `tests/fakes.py:FakeClock` is
one. `utcnow` is the real clock.
"""
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)


# Interval schedules that start here fire on a fixed grid (anchor + k * interval), so a process that
# didn't start the schedule can still tell when it fires next.
GRID_ANCHOR = datetime(2000, 1, 1, tzinfo=UTC)


def next_on_grid(after: datetime, every: timedelta, anchor: datetime = GRID_ANCHOR) -> datetime:
    """The first anchor + k * every at or after `after`: rounded up the way APScheduler's interval
    trigger picks its next fire time from its start_date."""
    return anchor - ((anchor - after) // every) * every
