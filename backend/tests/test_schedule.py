"""Adaptive per-board fetch cadence (ingest.schedule) and how ingest_boards / run_ingest use it."""
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import httpx
import pytest
import sqlalchemy as sa

from jobfinder.config import get_settings
from jobfinder.ingest import runner, schedule, service
from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.sources import ATS_FETCHERS
from jobfinder.models import Company, Job
from tests.fakes import FakeClock

LIMITS = {"base": 360, "min_minutes": 60, "max_minutes": 10080}
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# ── pure functions ─────────────────────────────────────────────────────
@pytest.mark.parametrize(("new_jobs", "tier", "expected"), [
    (0, 3, 10080), (0, None, 10080),       # quiet, untiered: weekly
    (1, 3, 1080), (7, 3, 360), (63, 3, 180), (1023, 3, 108),
    (0, 2, 1440), (7, 2, 180), (1023, 2, 60),
    (0, 1, 360),                           # a quiet coveted employer still waits no longer than base
    (7, 1, 90), (63, 1, 60), (5000, 1, 60),  # busy tier 1: the hourly floor
])
def test_board_interval_table(new_jobs, tier, expected):
    assert schedule.board_interval(new_jobs, tier, **LIMITS) == expected


@pytest.mark.parametrize("tier", [1, 2, 3, None])
def test_interval_never_grows_with_churn(tier):
    seq = [schedule.board_interval(n, tier, **LIMITS) for n in range(0, 2000, 7)]
    assert seq == sorted(seq, reverse=True)
    assert all(60 <= m <= 10080 for m in seq)


def test_higher_tier_is_never_slower():
    for n in (0, 1, 5, 50, 500):
        t1, t2, t3 = (schedule.board_interval(n, t, **LIMITS) for t in (1, 2, 3))
        assert t1 <= t2 <= t3


def test_user_boards_get_at_least_tier_two():
    assert schedule.effective_tier(None, "user") == 2
    assert schedule.effective_tier(3, "user") == 2
    assert schedule.effective_tier(1, "user") == 1
    assert schedule.effective_tier(None, "discovered") is None
    assert schedule.effective_tier(3, "seed") == 3


def test_failure_delay_doubles_and_caps():
    assert schedule.failure_delay(360, 0, 10080) == 360
    assert [schedule.failure_delay(360, f, 10080) for f in (1, 2, 3, 4, 5)] == [720, 1440, 2880, 5760, 10080]
    assert schedule.failure_delay(60, 50, 10080) == 10080


def test_jitter_is_deterministic_and_bounded():
    assert schedule.jitter_minutes(42, 360) == schedule.jitter_minutes(42, 360)
    spread = {schedule.jitter_minutes(i, 360) for i in range(1, 200)}
    assert all(0 <= j < 72 for j in spread) and len(spread) > 30  # boards drift apart
    nxt = schedule.next_fetch(7, 60, T0, 10080)
    assert T0 + timedelta(minutes=60) <= nxt < T0 + timedelta(minutes=73)
    # at the max the jitter is subtracted, so the cap holds but boards still spread out
    at_max = {schedule.next_fetch(i, 10080, T0, 10080) for i in range(1, 50)}
    assert all(T0 + timedelta(days=5) < t <= T0 + timedelta(minutes=10080) for t in at_max) and len(at_max) > 20


# ── ingest_boards with the adaptive schedule ─────────────────────────────
@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    async def nothing(client, slug, name):
        return []

    for ats in ATS_FETCHERS:
        monkeypatch.setitem(service.ATS_FETCHERS, ats, nothing)


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock(T0)
    monkeypatch.setattr(schedule, "now", c)
    return c


def postings(n, source="greenhouse"):
    return [JobPosting(source=source, external_id=str(i), company_name="X", title=f"Eng {i}",
                       url=f"https://x/{i}", description_text="d") for i in range(n)]


def returning(items=None, exc=None):
    async def fetch(client, slug, name):
        if exc:
            raise exc
        return items or []
    return fetch


async def boards(maker):
    async with maker() as db:
        return {c.slug: c for c in (await db.scalars(sa.select(Company))).all()}


@pytest.mark.real_commit
async def test_due_selection_with_a_frozen_clock(maker, monkeypatch, clock):
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning(postings(100)))
    monkeypatch.setitem(service.ATS_FETCHERS, "lever", returning(postings(1, "lever")))
    async with maker() as db:
        db.add_all([
            Company(name="Hot", ats="greenhouse", slug="hot", coveted_tier=1),
            Company(name="Quiet", ats="lever", slug="quiet"),
        ])
        await db.commit()
    # first run after deploy: next_fetch_at is NULL, so everything is fetched once
    assert sorted(r.company for r in await service.ingest_boards(maker)) == ["Hot", "Quiet"]
    b = await boards(maker)
    # a first batch is backlog, not churn: young boards start at the base cadence for their tier
    assert b["hot"].fetch_interval_minutes == 90  # tier 1
    assert b["quiet"].fetch_interval_minutes == 360  # untiered
    assert b["hot"].next_fetch_at >= T0 + timedelta(minutes=90)

    assert await service.ingest_boards(maker) == []  # nothing is due straight away
    clock.now = T0 + timedelta(minutes=110)  # past the tier-1 slot (90 min + up to 20% jitter)
    assert [r.company for r in await service.ingest_boards(maker)] == ["Hot"]
    clock.now = T0 + timedelta(days=1)  # Quiet's 18h slot has passed too
    assert sorted(r.company for r in await service.ingest_boards(maker)) == ["Hot", "Quiet"]


async def test_failure_backoff_pushes_next_fetch_out(maker, monkeypatch, clock):
    monkeypatch.setattr(get_settings(), "ingest_max_consecutive_failures", 0)  # never auto-disable here
    req = httpx.Request("GET", "https://x")
    gone = httpx.HTTPStatusError("x", request=req, response=httpx.Response(404, request=req))
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning(exc=gone))
    async with maker() as db:
        db.add(Company(name="Dead", ats="greenhouse", slug="dead", coveted_tier=1))
        await db.commit()
    gaps = []
    for _ in range(4):
        await service.ingest_boards(maker)
        dead = (await boards(maker))["dead"]
        gaps.append(dead.next_fetch_at - clock.now)
        clock.now = dead.next_fetch_at
    # a young tier-1 board = 90 min, doubled per consecutive failure; its jitter is a fixed share on top
    minutes = [g.total_seconds() / 60 for g in gaps]
    assert 180 <= minutes[0] < 180 * 1.2
    assert all(abs(b / a - 2) < 0.01 for a, b in pairwise(minutes))
    assert dead.consecutive_failures == 4 and dead.enabled


async def test_churn_counts_postings_after_the_backlog(maker, monkeypatch, clock):
    async with maker() as db:
        db.add(Company(name="Old", ats="greenhouse", slug="old"))
        await db.commit()
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning(postings(200)))
    await service.ingest_boards(maker)
    async with maker() as db:  # pretend that backlog arrived a month ago
        await db.execute(sa.update(Job).values(first_seen_at=sa.func.now() - timedelta(days=30)))
        await db.commit()
    clock.now = T0 + timedelta(days=1)
    await service.ingest_boards(maker, all_boards=True)
    assert (await boards(maker))["old"].fetch_interval_minutes == 10080  # nothing new for a week: weekly
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning(postings(263)))  # 63 new
    await service.ingest_boards(maker, all_boards=True)
    assert (await boards(maker))["old"].fetch_interval_minutes == 180


async def test_reenabling_a_board_makes_it_due_now(client, maker, make_account):
    async with maker() as db:
        c = Company(name="Back", ats="greenhouse", slug="back", enabled=False,
                    next_fetch_at=datetime.now(UTC) + timedelta(days=7))
        db.add(c)
        await db.commit()
    await make_account("owner@x.com", admin=True)
    assert (await client.post(f"/api/admin/sources/{c.id}/enable")).status_code == 200
    assert (await boards(maker))["back"].next_fetch_at is None


async def test_empty_guard_still_schedules_the_board(maker, monkeypatch, clock):
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning(postings(10)))
    async with maker() as db:
        db.add(Company(name="Flaky", ats="greenhouse", slug="flaky"))
        await db.commit()
    await service.ingest_boards(maker)
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", returning([]))
    clock.now = T0 + timedelta(days=2)
    [st] = await service.ingest_boards(maker)
    assert st.error.startswith("empty response")
    assert (await boards(maker))["flaky"].next_fetch_at > clock.now


async def test_explicit_slug_or_ats_bypasses_the_due_check(maker, clock):
    async with maker() as db:
        db.add(Company(name="Later", ats="greenhouse", slug="later", next_fetch_at=T0 + timedelta(days=3)))
        await db.commit()
    assert await service.ingest_boards(maker) == []
    assert [r.company for r in await service.ingest_boards(maker, slug="later")] == ["Later"]
    assert [r.company for r in await service.ingest_boards(maker, ats="greenhouse")] == ["Later"]


async def test_disabled_flag_keeps_the_old_behaviour(maker, monkeypatch, clock):
    monkeypatch.setattr(get_settings(), "adaptive_schedule_enabled", False)
    async with maker() as db:
        db.add(Company(name="Later", ats="greenhouse", slug="later", next_fetch_at=T0 + timedelta(days=3)))
        await db.commit()
    for _ in range(2):  # fetched every run, next slot untouched
        assert [r.company for r in await service.ingest_boards(maker)] == ["Later"]
    b = (await boards(maker))["later"]
    assert b.next_fetch_at == T0 + timedelta(days=3) and b.fetch_interval_minutes is None


async def test_jobspy_and_usajobs_keep_their_own_interval(maker, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "enable_jobspy", True)
    monkeypatch.setattr(s, "usajobs_api_key", "KEY")
    monkeypatch.setattr(s, "usajobs_email", "me@x.io")
    calls = []

    async def fake(source):
        async def run(session_factory, *a):
            calls.append(source)
            return service.IngestStats(source=source)
        return run

    monkeypatch.setattr(runner, "ingest_jobspy", await fake("jobspy"))
    monkeypatch.setattr(runner, "ingest_usajobs", await fake("usajobs"))
    for _ in range(3):  # three ticks in a row: each extra source runs once
        await runner.run_ingest(maker)
    assert calls == ["jobspy", "usajobs"]
    # an explicit request always runs
    await runner.run_ingest(maker, jobspy=True)
    await runner.run_ingest(maker, ats="usajobs")
    assert calls == ["jobspy", "usajobs", "jobspy", "usajobs"]
    await runner.run_ingest(maker, all_boards=True)  # the CLI's full run
    assert calls[-2:] == ["jobspy", "usajobs"] and len(calls) == 6
    # with the adaptive schedule off, every run is a full run, as before
    monkeypatch.setattr(s, "adaptive_schedule_enabled", False)
    await runner.run_ingest(maker)
    assert calls[-2:] == ["jobspy", "usajobs"] and len(calls) == 8
