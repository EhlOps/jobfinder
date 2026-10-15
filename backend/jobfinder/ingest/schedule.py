"""Adaptive per-board fetch cadence (REQ-26, #18 U14): how often each company board is polled.

The worker ticks every `ingest_tick_minutes`; each tick fetches only the boards that are due
(`next_fetch_at` is NULL or in the past). After every attempt the board's next slot is set from

    interval = clamp(base / (churn_factor × tier_weight), min, ceiling(tier))

  * base         `ingest_base_interval_minutes` (360: today's 6-hourly cadence).
  * churn_factor how busy the board is, from n = postings first seen for the company in the last
                 7 days (recomputed from `jobs.first_seen_at` on each run; nothing extra stored).
                 The board's first batch is its backlog, not churn, and is left out; a board with
                 less than 7 days of history counts as at least n = 7 (the base cadence), so a new
                 board is neither hammered nor parked for its first week:
                     churn_factor = max(base / max, log2(1 + n) / log2(1 + 7))
                 A posting a day (n = 7) gives 1, i.e. the base interval; every doubling of n
                 shortens the interval a step; a board with nothing new (n = 0) falls to the
                 quiet floor, which for a tier-3 board is exactly the max (weekly).
  * tier_weight  from `coveted_tier`: 1 → 4×, 2 → 2×, 3 / None → 1×. Boards users added by
                 pasting a link (origin 'user') get at least tier-2 treatment.
  * clamp        never below `ingest_min_interval_minutes` (60, the floor busy tier-1 boards
                 reach) and never above the tier's ceiling: tier 1 waits at most `base`, tier 2 at
                 most 4 × base, everything else at most `ingest_max_interval_minutes` (a week),
                 so a quiet coveted employer is still polled at least as often as before.

  With the defaults (minutes):   n=0    n=1   n=7   n=63   n≥1023
                        tier 3  10080   1080   360    180     108
                        tier 2   1440    540   180     90      60
                        tier 1    360    270    90     60      60

Failures: the existing backoff in service.py (consecutive_failures, auto-disable after N) is
unchanged; on top of it the next slot is pushed out exponentially, interval × 2^failures capped
at the max, so a dead board stops being hit every tick until it is disabled.

Spreading: each board's slot gets a fixed extra delay of 0–20% of its interval, from a hash of
its id (subtracted instead at the max, so the cap holds), so boards with the same interval
drift apart instead of all coming due on one tick.
First run after deploy: next_fetch_at is NULL for every board, so the first tick fetches all of
them once (exactly what every 6-hourly run did before); from then on they spread by interval
and jitter. Per-run caps (`select_boards`, now per tick) and per-host caps inside adapters still
apply. The CLI's `ingest` is a full run (every board, jobspy/usajobs ungated), as before.
Trade-off: a quiet board is also checked for closed postings only as often as it is fetched.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from jobfinder.core.clock import utcnow
from jobfinder.core.config import settings

CHURN_WINDOW = timedelta(days=7)
TIER_WEIGHT = {1: 4.0, 2: 2.0}  # anything else (3, None) weighs 1
FIRST_BATCH = timedelta(minutes=1)  # jobs first seen this close to a board's first job are its backlog
# INGEST_CHURN_PIVOT: new postings per window that earn the base interval (about one a day).
# INGEST_JITTER: up to this fraction of the interval is added, fixed per board.


def now() -> datetime:
    """The scheduler's clock (patched in tests)."""
    return utcnow()


def effective_tier(coveted_tier: int | None, origin: str | None) -> int | None:
    """A board a user pasted a link to is treated as at least tier 2."""
    if origin == "user":
        return min(coveted_tier or 3, 2)
    return coveted_tier


def churn_factor(new_jobs: int, base: int, max_minutes: int) -> float:
    quiet = base / max_minutes
    return max(quiet, math.log2(1 + max(new_jobs, 0)) / math.log2(1 + settings.ingest_churn_pivot))


def tier_ceiling(tier: int | None, base: int, max_minutes: int) -> int:
    if tier == 1:
        return min(base, max_minutes)
    if tier == 2:
        return min(4 * base, max_minutes)
    return max_minutes


def board_interval(
    new_jobs: int, tier: int | None, *, base: int, min_minutes: int, max_minutes: int
) -> int:
    """Minutes until the board should be fetched again after a normal run."""
    raw = base / (churn_factor(new_jobs, base, max_minutes) * TIER_WEIGHT.get(tier, 1.0))
    ceiling = max(tier_ceiling(tier, base, max_minutes), min_minutes)
    return round(min(max(raw, min_minutes), ceiling))


def failure_delay(interval: int, consecutive_failures: int, max_minutes: int) -> int:
    """Minutes until a failing board is tried again: doubles with each failure in a row."""
    if consecutive_failures <= 0:
        return interval
    return min(interval * 2 ** min(consecutive_failures, 16), max(max_minutes, interval))


def jitter_minutes(company_id: int, interval: int) -> int:
    """A fixed 0–INGEST_JITTER share of the interval, derived from the board id (Knuth's hash)."""
    frac = ((company_id * 2654435761) % 2**32) / 2**32
    return int(interval * settings.ingest_jitter * frac)


def next_fetch(company_id: int, delay_minutes: int, at: datetime, max_minutes: int) -> datetime:
    """`at` + delay + the board's jitter; at the max the jitter is taken off instead, so the cap holds."""
    j = jitter_minutes(company_id, delay_minutes)
    total = delay_minutes + j if delay_minutes + j <= max(max_minutes, delay_minutes) else delay_minutes - j
    return at + timedelta(minutes=total)
