"""tasks.brokers (#48): the stream connections keep no read timeout but detect a dead valkey, a
publish is bounded even when valkey hangs, and a worker's own broker is never started twice."""
import asyncio
import socket
import time

import pytest

from jobfinder.tasks import brokers


@pytest.fixture
def fresh(monkeypatch):
    """No broker started yet, a short publish bound, and valkey replaced by `kicked`."""
    monkeypatch.setattr(brokers, "_started", set())
    monkeypatch.setattr(brokers, "_lock", asyncio.Lock())
    monkeypatch.setattr(brokers, "PUBLISH_TIMEOUT", 0.2)
    kicked: list[bytes] = []

    async def kick(message):
        kicked.append(message)

    for broker in brokers.BROKERS.values():
        monkeypatch.setattr(broker, "kick", kick)
    return kicked


async def _hang():
    await asyncio.Event().wait()


@pytest.mark.skipif(not hasattr(socket, "TCP_USER_TIMEOUT"), reason="Linux keepalive options")
def test_stream_reads_survive_a_stalled_event_loop():
    """A 5s socket timeout killed worker-io mid-ingest on prod (the read times out while parsing blocks
    the loop); the brokers must not set one, and lean on TCP keepalive (~45s) for a dead valkey."""
    expected = {socket.TCP_KEEPIDLE: 30, socket.TCP_KEEPINTVL: 5, socket.TCP_KEEPCNT: 3,
                socket.TCP_USER_TIMEOUT: 45_000}
    for broker in brokers.BROKERS.values():
        kw = broker.connection_pool.connection_kwargs
        assert kw.get("socket_timeout") is None and kw.get("socket_keepalive") is True
        assert kw.get("socket_keepalive_options") == expected


async def test_a_hung_startup_times_out_and_does_not_keep_the_lock(fresh, monkeypatch):
    broker = brokers.ai_broker
    monkeypatch.setattr(broker, "startup", _hang)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await brokers.publish("ai", "cover_letter", 1)
    assert time.monotonic() - start < 1 and "ai" not in brokers._started and not brokers._lock.locked()

    started = []

    async def startup():
        started.append(True)

    monkeypatch.setattr(broker, "startup", startup)
    await brokers.publish("ai", "cover_letter", 2)  # valkey back: the next publish starts and sends
    assert started == [True] and "ai" in brokers._started and len(fresh) == 1


async def test_a_hung_send_times_out(fresh, monkeypatch):
    broker = brokers.io_broker
    monkeypatch.setattr(broker, "startup", lambda: asyncio.sleep(0))
    monkeypatch.setattr(broker, "kick", lambda message: _hang())
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await brokers.publish("io", "plan_work", 1)
    assert time.monotonic() - start < 1 and "io" in brokers._started


async def test_a_worker_publishing_to_its_own_queue_does_not_start_its_broker_again(fresh, monkeypatch):
    broker = brokers.io_broker
    monkeypatch.setattr(broker, "is_worker_process", True)  # what `taskiq worker` sets before startup

    async def startup():
        raise AssertionError("the taskiq worker already started this broker")

    monkeypatch.setattr(broker, "startup", startup)
    await brokers.publish("io", "plan_work", 1)
    assert len(fresh) == 1
