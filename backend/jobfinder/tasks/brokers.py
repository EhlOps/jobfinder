"""The three valkey-stream brokers (taskiq-redis; valkey speaks the Redis protocol) and `publish`.

A message carries only the ledger id: the worker claims the row and reads everything else from it.
Each kind is its own taskiq task name, registered on its queue's broker by @ledger_task. Building a
broker does no I/O; the first publish in a process starts it.
"""
import asyncio

from taskiq import AsyncBroker
from taskiq.kicker import AsyncKicker
from taskiq_redis import RedisStreamBroker

from jobfinder.core.config import settings

STREAM_MAXLEN = 10_000  # ids only; trimmed approximately, so valkey memory stays flat


def _broker(queue: str) -> RedisStreamBroker:
    return RedisStreamBroker(url=settings.valkey_url, queue_name=f"jobfinder:{queue}",
                             consumer_group_name=f"jobfinder-{queue}", maxlen=STREAM_MAXLEN)


ai_broker = _broker("ai")
io_broker = _broker("io")
render_broker = _broker("render")
BROKERS: dict[str, AsyncBroker] = {"ai": ai_broker, "io": io_broker, "render": render_broker}

_started: set[str] = set()
_lock = asyncio.Lock()


async def publish(queue: str, kind: str, task_id: int) -> None:
    """Send one ledger id to its queue's stream. Raises when valkey is unreachable (callers log it; the
    relay re-sends)."""
    broker = BROKERS[queue]
    if queue not in _started:
        async with _lock:
            if queue not in _started:
                await broker.startup()
                _started.add(queue)
    await AsyncKicker(task_name=kind, broker=broker, labels={}).kiq(task_id)
