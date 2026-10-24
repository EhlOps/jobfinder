"""The four valkey-stream brokers (taskiq-redis; valkey speaks the Redis protocol) and `publish`.

A message carries only the ledger id: the worker claims the row and reads everything else from it.
Each kind is its own taskiq task name, registered on its queue's broker by @ledger_task. Building a
broker does no I/O; the first publish in a process starts it.
"""
import asyncio
import socket

from taskiq import AsyncBroker, TaskiqEvents
from taskiq.kicker import AsyncKicker
from taskiq_redis import RedisStreamBroker

from jobfinder.core.config import settings
from jobfinder.tasks import diagnostics

STREAM_MAXLEN = 10_000  # ids only; trimmed approximately, so valkey memory stays flat
PUBLISH_TIMEOUT = 5  # seconds; a valkey that hangs rather than refuses must not stall the caller

# Idle 30s, then a probe every 5s, dead after 3 missed: a vanished valkey is noticed in ~45s. Probes
# only run on an idle connection; TCP_USER_TIMEOUT (ms) gives up on unacknowledged writes as quickly.
KEEPALIVE_OPTIONS = {getattr(socket, name): value
                     for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3),
                                         ("TCP_USER_TIMEOUT", 45_000))
                     if hasattr(socket, name)}


# The setting that caps each queue's tasks at once in one worker process (tasks/__main__.py).
CONCURRENCY = {"priority": "worker_priority_concurrency", "ai": "worker_ai_concurrency",
               "io": "worker_io_concurrency", "render": "renderer_concurrency"}


def _broker(queue: str) -> RedisStreamBroker:
    # No socket timeout: redis-py's default (5s) fires when a task's synchronous work (ingest parsing)
    # stalls the event loop past it, and taskiq then restarts the worker process, killing the tasks in
    # flight. TCP keepalive detects a dead peer instead (~45s); connecting keeps redis-py's 5s timeout.
    # xread_count: one XREADGROUP takes at most what the process runs at once (taskiq-redis's default is
    # 100). Everything read is pending on this consumer, and no other can claim it for 10 minutes, so a
    # big read-ahead would strand the unstarted rest when KEDA scales a pod in, and hide it from the
    # group's lag that KEDA scales the workers on (#52, #124, deploy/k8s/base/app/worker-*-scaledobject.yaml).
    return RedisStreamBroker(url=settings.valkey_url, queue_name=f"jobfinder:{queue}",
                             consumer_group_name=f"jobfinder-{queue}", maxlen=STREAM_MAXLEN,
                             xread_count=max(1, getattr(settings, CONCURRENCY[queue])),
                             socket_timeout=None, socket_keepalive=True,
                             socket_keepalive_options=KEEPALIVE_OPTIONS)


priority_broker = _broker("priority")
ai_broker = _broker("ai")
io_broker = _broker("io")
render_broker = _broker("render")
BROKERS: dict[str, AsyncBroker] = {"priority": priority_broker, "ai": ai_broker,
                                    "io": io_broker, "render": render_broker}
# WORKER_STARTUP fires only in taskiq's worker child, so the api and the scheduler never install these.
for _b in BROKERS.values():
    _b.add_event_handler(TaskiqEvents.WORKER_STARTUP, diagnostics.on_worker_startup)  # stack dumps (#136)

_started: set[str] = set()
_lock = asyncio.Lock()


async def publish(queue: str, kind: str, task_id: int) -> None:
    """Send one ledger id to its queue's stream within PUBLISH_TIMEOUT. Raises when valkey is
    unreachable or hangs (callers log it; the relay re-sends)."""
    broker = BROKERS[queue]
    async with asyncio.timeout(PUBLISH_TIMEOUT):
        # A worker's own broker is started by taskiq; starting it again would re-fire WORKER_STARTUP.
        if queue not in _started and not broker.is_worker_process:
            async with _lock:  # the timeout bounds a hung startup too, so it cannot keep the lock
                if queue not in _started:
                    await broker.startup()
                    _started.add(queue)
        await AsyncKicker(task_name=kind, broker=broker, labels={}).kiq(task_id)
