"""The Claude gate (integrations/claude/gate.py) against a real valkey: TEST_VALKEY_URL, by default the
one `jobfinder dev` port-forwards to :6380. Each test has its own key prefix. Outside CI the valkey tests
skip when it can't be reached; in CI (which runs a valkey service) they fail instead."""
import asyncio
import os
import time
import uuid

import pytest
import redis.asyncio as aioredis
from stub_claude import StubClaude, envelope

from jobfinder.integrations.claude import gate
from jobfinder.integrations.claude.claude_code import AIError, ClaudeCode
from tests.conftest import TEST_VALKEY_URL


@pytest.fixture
async def vk():
    r = aioredis.Redis.from_url(TEST_VALKEY_URL, socket_connect_timeout=2)
    try:
        await asyncio.wait_for(r.ping(), 2)
    except Exception as e:
        await r.aclose()
        if os.environ.get("CI"):
            raise
        pytest.skip(f"no valkey at {TEST_VALKEY_URL} ({type(e).__name__}); start it with `jobfinder dev up --deps-only`")
    prefix = f"jobfinder:test:gate:{uuid.uuid4().hex}"
    gates: list[gate.ClaudeGate] = []

    def make(lane="bulk", limit=1):
        g = gate.ClaudeGate(limit=limit, url=TEST_VALKEY_URL, prefix=prefix, lane_of=lambda: lane)
        gates.append(g)
        return g

    make.r, make.prefix = r, prefix
    yield make
    for g in gates:
        await g.aclose()
    await r.delete(f"{prefix}:permits", f"{prefix}:waiting")
    await r.aclose()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gate, "POLL", 0.02)


async def _vk_now(r) -> float:
    sec, usec = await r.time()
    return sec + usec / 1e6


async def test_limit_holds_across_gates(vk):
    gates = [vk(limit=2) for _ in range(3)]  # three "processes", each with its own client
    held, peak = 0, 0

    async def call(g):
        nonlocal held, peak
        async with g.slot(30):
            held += 1
            peak = max(peak, held)
            assert await vk.r.zcard(f"{vk.prefix}:permits") <= 2
            await asyncio.sleep(0.05)
            held -= 1

    await asyncio.gather(*(call(g) for g in gates for _ in range(4)))
    assert peak == 2
    assert await vk.r.zcard(f"{vk.prefix}:permits") == 0  # every permit released


async def test_priority_beats_an_earlier_bulk_waiter(vk):
    order: list[str] = []
    release = asyncio.Event()

    async def holder():
        async with vk("bulk").slot(30):
            await release.wait()

    async def caller(name, lane):
        async with vk(lane).slot(30):
            order.append(name)

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.1)
    bulk = asyncio.create_task(caller("bulk", "bulk"))
    await asyncio.sleep(0.1)  # bulk has been waiting a while before priority arrives
    prio = asyncio.create_task(caller("priority", "priority"))
    await asyncio.sleep(0.1)
    release.set()
    await asyncio.gather(h, bulk, prio)
    assert order == ["priority", "bulk"]


async def test_bulk_leaves_a_free_permit_while_priority_waits(vk):
    # A priority caller in another process is waiting (its mark is fresh): bulk must not take the free permit.
    await vk.r.zadd(f"{vk.prefix}:waiting", {"other": await _vk_now(vk.r) + 30})
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            async with vk("bulk", limit=2).slot(30):
                pass
    assert await vk.r.zcard(f"{vk.prefix}:permits") == 0
    async with vk("priority", limit=2).slot(30):  # priority still gets it at once
        pass


async def test_a_dead_waiter_mark_expires(vk):
    await vk.r.zadd(f"{vk.prefix}:waiting", {"dead": await _vk_now(vk.r) + 0.3})
    t0 = time.monotonic()
    async with vk("bulk").slot(30):
        waited = time.monotonic() - t0
    assert 0.2 <= waited < 2


async def test_a_priority_waiter_renews_its_mark(vk, monkeypatch):
    monkeypatch.setattr(gate, "WAIT_MARK", 0.2)
    release = asyncio.Event()

    async def holder():
        async with vk("bulk").slot(30):
            await release.wait()

    async def prio():
        async with vk("priority").slot(30):
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.05)
    p = asyncio.create_task(prio())
    await asyncio.sleep(0.6)  # three marks' worth: still marked, because it re-marks every poll
    assert await vk.r.zcard(f"{vk.prefix}:waiting") == 1
    release.set()
    await asyncio.gather(h, p)
    assert await vk.r.zcard(f"{vk.prefix}:waiting") == 0


async def test_an_expired_lease_frees_its_permit(vk):
    stuck = asyncio.Event()

    async def dead_holder():  # holds past its lease, like a pod killed mid-call
        async with vk().slot(0.3):
            stuck.set()
            await asyncio.sleep(30)

    d = asyncio.create_task(dead_holder())
    await stuck.wait()
    t0 = time.monotonic()
    async with vk().slot(30):
        waited = time.monotonic() - t0
    assert 0.1 <= waited < 2
    d.cancel()


async def test_no_permit_within_the_gate_timeout_fails_closed(vk, monkeypatch):
    monkeypatch.setattr(gate, "GATE_TIMEOUT", 0.3)
    async with vk().slot(30):
        with pytest.raises(gate.GateUnavailable):
            async with vk().slot(30):
                pass
    assert await vk.r.zcard(f"{vk.prefix}:permits") == 0


async def test_unreachable_valkey_fails_closed():
    g = gate.ClaudeGate(limit=2, url="redis://127.0.0.1:1/0")  # nothing listens on port 1
    with pytest.raises(gate.GateUnavailable):
        async with g.slot(30):
            pass
    await g.aclose()


async def test_claude_code_turns_a_closed_gate_into_a_retryable_timeout(tmp_path):
    stub = StubClaude(tmp_path, [{"stdout": envelope(result="never runs")}])
    g = gate.ClaudeGate(limit=2, url="redis://127.0.0.1:1/0")
    records = []

    async def record(r):
        records.append(r)

    ai = ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path), oauth_token="t", claude_gate=g,
                    recorder=record)
    with pytest.raises(AIError) as e:
        await ai.run("p", system="s")
    assert e.value.kind == "timeout" and e.value.retryable
    assert stub.calls == [] and records == []  # no permit: no CLI, and no ai_calls row to end a backoff early
    await g.aclose()


async def test_claude_code_leases_cover_the_repair_retry(tmp_path, local_claude_gate):
    stub = StubClaude(tmp_path, [{"stdout": envelope(result="hi")}])
    await ClaudeCode(claude_bin=str(stub.path), scratch_dir=str(tmp_path), oauth_token="t").run(
        "p", system="s", timeout_s=100)
    assert local_claude_gate.leases == [2 * 100 + 30]
