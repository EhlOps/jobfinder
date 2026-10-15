"""Time. Code asks a clock for "now" instead of calling datetime.now(), so tests can fix the time.

A clock is any zero-argument callable returning an aware UTC datetime; `tests/fakes.py:FakeClock` is
one. `utcnow` is the real clock.
"""
from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """The current time, timezone-aware, in UTC."""
    return datetime.now(UTC)
