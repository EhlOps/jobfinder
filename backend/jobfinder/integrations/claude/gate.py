"""ClaudeGate: the cluster's Claude call permits, shared by every process through valkey (#124).

`AI_MAX_CONCURRENCY` calls run at once in total, whatever the number of worker pods, and a priority
caller gets the next free permit before any bulk caller. Nothing is reserved: with no priority caller
waiting, bulk takes every permit.

Two sorted sets hold the state. `<prefix>:permits` maps each holder to its lease expiry, and
`<prefix>:waiting` maps each waiting priority caller to the expiry of its short "still waiting" mark.
One Lua script acquires atomically: purge what has expired, grant if a permit is free and the caller is
priority or no priority caller is waiting, otherwise (priority only) renew the caller's waiting mark.
Callers poll every POLL seconds, so a priority waiter re-marks itself each poll, and one that dies drops
out after WAIT_MARK. Release is a ZREM; a holder that dies without releasing frees its permit when its
lease runs out. Times come from valkey's own clock (`TIME` inside the script), so callers' clocks never
need to agree.

The lane is fixed per process (`set_lane`): the api and the priority worker are `priority`, everything
else `bulk`. The gate fails closed: when valkey is unreachable, or a permit doesn't come within
GATE_TIMEOUT, `slot` raises GateUnavailable (ClaudeCode turns it into a retryable AIError "timeout",
so the task retries with backoff; not "rate_limited", which would back the planner off). A per-pod
fallback would break the cap once worker pods autoscale, and with valkey down nothing is delivered anyway.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Literal, Protocol

import redis.asyncio as aioredis
from redis.commands.core import AsyncScript
from redis.exceptions import RedisError

from jobfinder.core.config import settings
from jobfinder.tasks.brokers import KEEPALIVE_OPTIONS

Lane = Literal["priority", "bulk"]

PREFIX = "jobfinder:claude"
WAIT_MARK = 5  # seconds a priority waiter's mark lasts unless renewed; a dead waiter blocks bulk this long
POLL = 0.25  # seconds between acquire attempts
GATE_TIMEOUT = 600  # seconds a call may wait for a permit (bulk behind priority work) before its task retries
CALL_TIMEOUT = 5  # seconds for one valkey round trip; a hung valkey must not hold a caller forever

# KEYS: permits, waiting. ARGV: holder, lane, limit, lease seconds, wait-mark seconds. Returns 1 if granted.
ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local priority = ARGV[2] == 'priority'
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) and (priority or redis.call('ZCARD', KEYS[2]) == 0) then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[4]), ARGV[1])
  redis.call('ZREM', KEYS[2], ARGV[1])
  return 1
end
if priority then redis.call('ZADD', KEYS[2], now + tonumber(ARGV[5]), ARGV[1]) end
return 0
"""


class GateUnavailable(Exception):
    """No permit: valkey is unreachable or hung, or none came free within GATE_TIMEOUT."""


class Gate(Protocol):
    def slot(self, lease_s: float) -> contextlib.AbstractAsyncContextManager[None]: ...


_lane: Lane = "bulk"


def set_lane(lane: Lane) -> None:
    """Set this process's lane. Call it before the first Claude call (a forked worker inherits it)."""
    global _lane
    _lane = lane


def lane() -> Lane:
    return _lane


class ClaudeGate:
    def __init__(self, *, limit: int, url: str, prefix: str = PREFIX, lane_of=lane):
        self._limit, self._url, self._lane_of = limit, url, lane_of
        self._keys = [f"{prefix}:permits", f"{prefix}:waiting"]
        self._redis: aioredis.Redis | None = None
        self._acquire: AsyncScript | None = None

    def _client(self) -> aioredis.Redis:
        # Built on first use, so creating the gate does no I/O. TCP keepalive as in tasks/brokers.py;
        # each call is bounded by CALL_TIMEOUT instead of a socket timeout.
        if self._redis is None:
            self._redis = aioredis.Redis.from_url(
                self._url, socket_connect_timeout=CALL_TIMEOUT, socket_keepalive=True,
                socket_keepalive_options=KEEPALIVE_OPTIONS)
            self._acquire = self._redis.register_script(ACQUIRE)  # EVALSHA, loading the script on a miss
        return self._redis

    async def _forget(self, holder: str) -> None:
        # Best effort: a lost ZREM costs at most one lease (permit) or one WAIT_MARK (waiting mark).
        with contextlib.suppress(Exception):
            async with asyncio.timeout(CALL_TIMEOUT):
                await self._client().zrem(self._keys[0], holder)
                await self._client().zrem(self._keys[1], holder)

    @contextlib.asynccontextmanager
    async def slot(self, lease_s: float) -> AsyncIterator[None]:
        """Hold one permit for the block. `lease_s` must cover the block's longest run: past it, the
        permit counts as free again."""
        holder, lane_ = uuid.uuid4().hex, self._lane_of()
        args = [holder, lane_, self._limit, lease_s, WAIT_MARK]
        self._client()  # builds the client and its script on first use; no I/O
        try:
            async with asyncio.timeout(GATE_TIMEOUT):
                while True:
                    async with asyncio.timeout(CALL_TIMEOUT):
                        if await self._acquire(keys=self._keys, args=args):
                            break
                    await asyncio.sleep(POLL)
        except (RedisError, OSError, TimeoutError) as e:
            # A reply lost after the script ran would leave a permit or mark behind: drop it now.
            await self._forget(holder)
            raise GateUnavailable(f"no Claude permit ({lane_}): {type(e).__name__} {e}".strip()) from e
        except BaseException:  # cancelled while waiting
            await self._forget(holder)
            raise
        try:
            yield
        finally:
            await self._forget(holder)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = self._acquire = None


_default: ClaudeGate | None = None


def default_gate() -> Gate:
    """The process's gate on VALKEY_URL with AI_MAX_CONCURRENCY permits, built on first use."""
    global _default
    if _default is None:
        _default = ClaudeGate(limit=settings.ai_max_concurrency, url=settings.valkey_url)
    return _default
