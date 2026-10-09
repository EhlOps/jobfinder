from datetime import UTC, datetime, timedelta

import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder import worker
from jobfinder.config import get_settings
from jobfinder.models import AICall, Job, PlannerState, Profile, Task, User
from jobfinder.scheduling import planner, queue
from jobfinder.scheduling.handlers import HANDLERS
from tests.factories import make_match, make_user


def test_scheduler_has_ingest_and_daily_email_jobs(monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_interval_hours", 3)
    sched = worker.make_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"ingest_jobs", "daily_emails", "plan_work"}
    assert jobs["ingest_jobs"].args == ("ingest_jobs",) and jobs["daily_emails"].args == ("daily_emails",)
    assert "interval[3:00:00]" in str(jobs["ingest_jobs"].trigger)
    assert "minute='5'" in str(jobs["daily_emails"].trigger) and "hour" not in str(jobs["daily_emails"].trigger).replace("minute", "")


async def test_scheduled_work_goes_through_the_queue_without_piling_up(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", maker)
    await worker.enqueue_scheduled("ingest_jobs")
    await worker.enqueue_scheduled("ingest_jobs")            # previous run still queued: not duplicated
    await worker.enqueue_scheduled("daily_emails")
    async with maker() as db:
        kinds = sorted((await db.scalars(sa.select(Task.kind))).all())
    assert kinds == ["daily_emails", "ingest_jobs"]


def test_every_scheduled_kind_has_a_handler():
    assert {"ingest_jobs", "daily_emails", "collect_questions", "match_user"} <= set(HANDLERS)


# ---- background planner ----------------------------------------------------------------------------

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def maker(engine, monkeypatch):
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(get_settings(), "planner_max_per_tick", 3)
    monkeypatch.setattr(get_settings(), "planner_stagger_seconds", 120)
    return m


async def _setup(db, email, *, seen_ago=timedelta(hours=1), last_run_ago=timedelta(hours=10), version=1, changed=False):
    """An active user whose last match run was `last_run_ago` ago, with one match scored against version 1."""
    u = await make_user(db, email, version=2 if changed else version)
    u.last_seen_at = NOW - seen_ago
    p = await db.get(Profile, u.id)
    if last_run_ago is not None:
        p.match_summary = {"last_run_at": (NOW - last_run_ago).isoformat(), "scored": 1, "pending": 0}
    p.audit = {"version": p.version}  # already audited, so a match run is what gets queued
    await make_match(db, u.id, collected=True, version=1)
    await db.execute(sa.update(Job).values(first_seen_at=NOW - timedelta(days=30)))
    await db.commit()
    return u


async def _tasks(db):
    return (await db.scalars(sa.select(Task).order_by(Task.run_after, Task.id))).all()


def test_scheduler_has_planner_job():
    assert "plan_work" in {j.id for j in worker.make_scheduler().get_jobs()}
    assert "plan_work" in HANDLERS


async def test_profile_change_first_then_longest_waiting(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "planner_max_per_tick", 10)
    async with maker() as db:
        stale = await _setup(db, "stale@x.com", last_run_ago=timedelta(hours=30))
        recent = await _setup(db, "recent@x.com", last_run_ago=timedelta(hours=10))
        changed = await _setup(db, "changed@x.com", changed=True, last_run_ago=timedelta(hours=5))
        db.add(Job(source="greenhouse", external_id="n", company_name="A", title="T", url="https://j/1", dedupe_hash="n",
                   first_seen_at=NOW - timedelta(hours=1)))
        await db.commit()
        result = await planner.plan(db, NOW)
        order = [t.user_id for t in await _tasks(db) if t.kind == "match_user"]
    assert result["queued"]["match_user"] == 3
    assert order == [changed.id, stale.id, recent.id]  # a profile change outranks everything; then who waited longest


async def test_cap_and_stagger(maker):
    async with maker() as db:
        for i in range(5):
            await _setup(db, f"u{i}@x.com", last_run_ago=timedelta(hours=30))
        await planner.plan(db, NOW)
        tasks = [t for t in await _tasks(db) if t.kind == "match_user"]
        states = (await db.scalars(sa.select(PlannerState).where(PlannerState.last_skip == "waiting for a free slot"))).all()
    assert len(tasks) == 3 and len(states) == 2
    assert [t.run_after for t in tasks] == [NOW + timedelta(seconds=120 * i) for i in range(3)]


async def test_work_already_in_flight_uses_up_slots(maker):
    async with maker() as db:
        users = [await _setup(db, f"u{i}@x.com", last_run_ago=timedelta(hours=30)) for i in range(4)]
        await queue.enqueue(db, "match_user", user_id=users[0].id)
        await queue.enqueue(db, "audit_profile", user_id=users[1].id)
        await planner.plan(db, NOW)
        planned = {t.user_id for t in await _tasks(db)}
    assert planned == {users[0].id, users[1].id, users[2].id}  # one free slot, goes to the next user


async def test_budget_exhausted_users_are_skipped(maker):
    async with maker() as db:
        u = await _setup(db, "a@x.com", last_run_ago=timedelta(hours=30))
        (await db.get(User, u.id)).match_budget = 0
        await db.commit()
        result = await planner.plan(db, NOW)
        state = await db.get(PlannerState, u.id)
    assert result["queued"]["match_user"] == 0 and state.last_skip == "budget exhausted"


async def test_idle_users_are_throttled(maker):
    async with maker() as db:
        idle = await _setup(db, "idle@x.com", seen_ago=timedelta(days=20), last_run_ago=timedelta(days=2))
        active = await _setup(db, "active@x.com", seen_ago=timedelta(days=1), last_run_ago=timedelta(hours=4))
        await planner.plan(db, NOW)
        planned = {t.user_id for t in await _tasks(db)}
        state = await db.get(PlannerState, idle.id)
    # The active user ran 4h ago (past the 3h gap) but has nothing new and is not stale; the idle one is held to 7 days.
    assert planned == set() and state.last_skip == "inactive user, throttled"
    assert state.next_due_at == NOW + timedelta(days=5)
    async with maker() as db:
        db.add(Job(source="greenhouse", external_id="x", company_name="A", title="T", url="https://j/2", dedupe_hash="x",
                   first_seen_at=NOW - timedelta(minutes=30)))
        await db.commit()
        await planner.plan(db, NOW)
        planned = {t.user_id for t in await _tasks(db)}
    assert planned == {active.id}


async def test_recently_planned_users_wait_for_the_gap(maker):
    async with maker() as db:
        u = await _setup(db, "a@x.com", last_run_ago=timedelta(hours=30))
        await planner.plan(db, NOW)
        for t in await _tasks(db):
            await queue.complete(db, t, {})
        await planner.plan(db, NOW + timedelta(minutes=15))  # still stale, but planned a moment ago
        assert len(await _tasks(db)) == 1
        await planner.plan(db, NOW + timedelta(hours=4))
        assert len(await _tasks(db)) == 2
    assert u.id


async def test_rate_limit_backs_off_the_whole_tick_and_recovers(maker):
    async with maker() as db:
        await _setup(db, "a@x.com", last_run_ago=timedelta(hours=30))
        db.add(AICall(task="match", model="sonnet", duration_ms=1, ok=False, error_kind="rate_limited", created_at=NOW - timedelta(minutes=5)))
        await db.commit()
        assert (await planner.plan(db, NOW))["skipped"] == "rate_limited"
        assert await _tasks(db) == []
        assert (await planner.plan(db, NOW + timedelta(minutes=40)))["queued"]["match_user"] == 1  # window passed
        db.add(AICall(task="match", model="sonnet", duration_ms=1, ok=True, created_at=NOW + timedelta(minutes=41)))
        await db.commit()
        assert await planner.backoff_until(db, NOW + timedelta(minutes=42)) is None


async def test_stale_profile_audit_replaces_the_match_run_and_questions_catch_up(maker):
    async with maker() as db:
        u = await _setup(db, "a@x.com", last_run_ago=timedelta(hours=30), changed=True)
        (await db.get(Profile, u.id)).audit = {"version": 1}
        await make_match(db, u.id, collected=False, version=2)
        await planner.plan(db, NOW)
        kinds = sorted(t.kind for t in await _tasks(db))
    assert kinds == ["audit_profile", "collect_questions"]


async def test_pending_and_unfinished_users_are_never_planned(maker):
    async with maker() as db:
        await _setup(db, "ok@x.com", last_run_ago=timedelta(hours=30))
        await make_user(db, "blank@x.com", status={})
        pend = await make_user(db, "pend@x.com")
        (await db.get(User, pend.id)).password_hash = None
        await db.commit()
        result = await planner.plan(db, NOW)
    assert result["considered"] == 1


async def test_ingest_queues_one_planner_run_instead_of_one_per_user(maker, monkeypatch):
    from jobfinder.ingest import runner

    async def fake_ingest(_):
        return {"new": 0}

    monkeypatch.setattr(runner, "run_ingest", fake_ingest)
    async with maker() as db:
        for i in range(3):
            await make_user(db, f"u{i}@x.com")
        task = await queue.enqueue(db, "ingest_jobs")
        await HANDLERS["ingest_jobs"](db, None, task)
        kinds = sorted(t.kind for t in await _tasks(db))
    assert kinds == ["ingest_jobs", "plan_work"]


async def test_last_seen_is_written_at_most_every_ten_minutes(authed, engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def seen():
        async with maker() as db:
            return (await db.scalars(sa.select(User.last_seen_at))).one()

    await authed.get("/api/auth/me")
    first = await seen()
    assert first is not None
    await authed.get("/api/auth/me")
    assert await seen() == first


async def test_admin_schedule_endpoint(authed, engine, maker, monkeypatch):
    assert (await authed.get("/api/admin/schedule")).status_code == 403
    monkeypatch.setattr(get_settings(), "admin_emails", "sam@example.com")
    r = await authed.get("/api/admin/schedule")
    assert r.status_code == 200
    body = r.json()
    assert body["max_per_tick"] == 3 and body["backoff_until"] is None
    assert [u["email"] for u in body["users"]] == ["sam@example.com"]
    assert {"last_run_at", "next_due_at", "budget_left", "reason", "skip", "active"} <= set(body["users"][0])


async def test_a_passed_graduation_date_gets_the_user_planned_again(maker):
    async with maker() as db:
        u = await _setup(db, "grad@x.com", last_run_ago=timedelta(hours=5))
        p = await db.get(Profile, u.id)
        p.status = {**p.status, "graduation_date": "2026-06"}
        await db.commit()
        # First look: records the stage ("final_year" as of NOW = 2026-06-01) without re-scoring anyone.
        await planner.plan(db, NOW)
        assert (await db.get(Profile, u.id)).career_stage == "final_year"
        assert (await db.get(Profile, u.id)).version == 1
        assert [t for t in await _tasks(db) if t.kind == "match_user"] == []
        # Months later the date has passed: the stage changes, the version bumps and the user is planned.
        later = NOW + timedelta(days=200)
        p = await db.get(Profile, u.id)
        p.match_summary = {**p.match_summary, "last_run_at": (later - timedelta(hours=5)).isoformat()}
        (await db.get(User, u.id)).last_seen_at = later - timedelta(hours=1)      # still an active user
        await db.commit()
        await planner.plan(db, later)
        assert (await db.get(Profile, u.id)).career_stage in ("recent_grad", "graduated")
        # The bump came from the stage sync alone, so no AI audit is queued; the match run is.
        assert [(t.kind, t.user_id) for t in await _tasks(db)] == [("match_user", u.id)]


async def test_a_failing_audit_does_not_block_matching(maker):
    async with maker() as db:
        u = await _setup(db, "a@x.com", last_run_ago=timedelta(hours=30), changed=True)
        (await db.get(Profile, u.id)).audit = {"version": 1}
        await db.commit()
        await planner.plan(db, NOW)
        assert [t.kind for t in await _tasks(db)] == ["audit_profile"]
        await db.execute(sa.update(Task).values(status="failed", finished_at=NOW))
        (await db.get(PlannerState, u.id)).last_planned_at = None
        await db.commit()
        await planner.plan(db, NOW + timedelta(hours=1))
        assert sorted(t.kind for t in await _tasks(db)) == ["audit_profile", "match_user"]
        # Long after the failure the audit is tried again.
        await db.execute(sa.update(Task).values(status="done"))
        (await db.get(PlannerState, u.id)).last_planned_at = None
        await db.commit()
        await planner.plan(db, NOW + timedelta(hours=10))
        assert [t.kind for t in await _tasks(db)].count("audit_profile") == 2


async def test_new_job_counts_are_per_user_last_run(maker):
    async with maker() as db:
        a = await _setup(db, "a@x.com", last_run_ago=timedelta(hours=10))
        b = await _setup(db, "b@x.com", last_run_ago=timedelta(hours=2))
        c = await _setup(db, "c@x.com", last_run_ago=None)
        job = (await db.scalars(sa.select(Job).limit(1))).one()
        await db.execute(sa.update(Job).where(Job.id == job.id).values(first_seen_at=NOW - timedelta(hours=5)))
        await db.commit()
        views = {v.user.id: v for v in await planner._views(db, NOW)}
        total = await db.scalar(sa.select(sa.func.count()).select_from(Job).where(Job.is_active.is_(True)))
    assert views[a.id].new_jobs == 1
    assert views[b.id].new_jobs == 0
    assert views[c.id].new_jobs == total


async def test_save_state_upserts_in_one_statement_and_keeps_last_planned(maker):
    async with maker() as db:
        a = await _setup(db, "a@x.com")
        b = await _setup(db, "b@x.com")
        views = await planner._views(db, NOW)
        for v in views:
            v.next_due_at, v.reason, v.skip = NOW, "r1", ""
        await planner._save_state(db, views, NOW, {a.id})
        for v in views:
            v.reason, v.skip = "", "s2"
        await planner._save_state(db, views, NOW + timedelta(hours=1), {b.id})
        states = {s.user_id: s for s in await db.scalars(sa.select(PlannerState))}
    assert (states[a.id].last_planned_at, states[a.id].last_skip) == (NOW, "s2")
    assert (states[b.id].last_planned_at, states[b.id].last_reason) == (NOW + timedelta(hours=1), "")
