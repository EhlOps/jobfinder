"""Job links, coverage and admin after the service port (#46): one commit per request, so a failure after
the first write keeps nothing, and the list endpoints cost the same number of queries for 1 row or many.

Paths that queue a task failing after the add are tested in test_enqueue_rollback."""
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from jobfinder.config import get_settings
from jobfinder.models import Company, CovetedTarget, JobLink, JobMatch, RateEvent, User
from jobfinder.repositories import coverage as coverage_repo
from jobfinder.repositories import job_links as links_repo
from jobfinder.services import admin as admin_service
from tests.rollback import Boom, count, raise_after

GH = "https://boards.greenhouse.io/acme/jobs/1"


@pytest.fixture
def links_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "job_links_enabled", True)


@contextmanager
def queries(maker):
    """Count the SQL statements sent while the block runs."""
    engine = maker.kw["bind"].sync_engine
    n = [0]

    def bump(*_):
        n[0] += 1

    sa.event.listen(engine, "before_cursor_execute", bump)
    try:
        yield n
    finally:
        sa.event.remove(engine, "before_cursor_execute", bump)


async def _company(maker, slug="acme", **kw) -> int:
    async with maker() as db:
        c = Company(name=slug.title(), ats="greenhouse", slug=slug, **kw)
        db.add(c)
        await db.commit()
        return c.id


# --- rollback: nothing a failed request wrote is kept --------------------------------------------

async def test_add_link_failing_after_the_insert_keeps_nothing(authed, maker, monkeypatch, links_on):
    await _company(maker)  # a known board: resolved at once, no task
    raise_after(monkeypatch, links_repo, "add")
    with pytest.raises(Boom):
        await authed.post("/api/job-links", json={"url": GH})
    assert await count(maker, JobLink) == 0
    assert await count(maker, RateEvent, kind="job_link") == 0


async def test_add_known_board_answers_201_resolved(authed, maker, links_on):
    cid = await _company(maker)
    r = await authed.post("/api/job-links", json={"url": GH})
    assert r.status_code == 201
    assert r.json()["status"] == "resolved" and r.json()["company_id"] == cid
    assert await count(maker, JobLink, company_id=cid) == 1


async def test_delete_link_failing_keeps_the_link(authed, maker, monkeypatch, links_on):
    await _company(maker)
    link_id = (await authed.post("/api/job-links", json={"url": GH})).json()["id"]
    raise_after(monkeypatch, links_repo, "delete")
    with pytest.raises(Boom):
        await authed.delete(f"/api/job-links/{link_id}")
    assert await count(maker, JobLink, id=link_id) == 1


async def test_pin_company_failing_keeps_it_unpinned(client, maker, make_account, monkeypatch):
    cid = await _company(maker, coveted_tier=3, coveted_score=10)
    await make_account("owner@x.com", admin=True)
    raise_after(monkeypatch, admin_service, "_pin_company")
    with pytest.raises(Boom):
        await client.post("/api/admin/coverage/pin", json={"company_id": cid})
    assert await count(maker, Company, id=cid, coveted_pinned=None) == 1


async def test_pin_new_employer_failing_adds_no_target(client, maker, make_account, monkeypatch):
    await make_account("owner@x.com", admin=True)
    raise_after(monkeypatch, coverage_repo, "add")
    with pytest.raises(Boom):
        await client.post("/api/admin/coverage/pin", json={"name": "Acme", "domain": "acme.com"})
    assert await count(maker, CovetedTarget) == 0


async def test_unpin_target_failing_keeps_the_target(client, maker, make_account, monkeypatch):
    async with maker() as db:
        t = CovetedTarget(name="Wanted", domain="wanted.com", source="admin", score=100)
        db.add(t)
        await db.commit()
    await make_account("owner@x.com", admin=True)
    raise_after(monkeypatch, coverage_repo, "delete")
    with pytest.raises(Boom):
        await client.post("/api/admin/coverage/unpin", json={"target_id": t.id})
    assert await count(maker, CovetedTarget, id=t.id) == 1


async def test_unpin_company_failing_to_queue_the_rescore_keeps_the_pin(client, maker, make_account, monkeypatch):
    cid = await _company(maker, coveted_tier=1, coveted_score=50, coveted_pinned=True)
    await make_account("owner@x.com", admin=True)

    async def boom(*a, **kw):
        raise Boom("ledger.add")
    monkeypatch.setattr(admin_service.ledger, "add", boom)
    with pytest.raises(Boom):
        await client.post("/api/admin/coverage/unpin", json={"company_id": cid, "exclude": True})
    assert await count(maker, Company, id=cid, coveted_pinned=True) == 1


async def test_enable_source_failing_keeps_it_disabled(client, maker, make_account, monkeypatch):
    cid = await _company(maker, enabled=False, consecutive_failures=5, disabled_reason="5 consecutive failures")
    await make_account("owner@x.com", admin=True)
    raise_after(monkeypatch, admin_service, "enable_source")
    with pytest.raises(Boom):
        await client.post(f"/api/admin/sources/{cid}/enable")
    assert await count(maker, Company, id=cid, enabled=False, consecutive_failures=5) == 1


# --- no N+1: the query count does not grow with the rows -----------------------------------------

async def _list_queries(authed, maker) -> int:
    with queries(maker) as n:
        r = await authed.get("/api/job-links")
    assert r.status_code == 200
    return n[0]


async def test_job_links_list_queries_do_not_grow_with_links(authed, maker, links_on):
    async with maker() as db:
        uid = await db.scalar(sa.select(User.id))
    await _company(maker, "first")
    await authed.post("/api/job-links", json={"url": "https://boards.greenhouse.io/first/jobs/1"})
    await _list_queries(authed, maker)  # warm up: per-session first-request work is not per link
    one = await _list_queries(authed, maker)
    async with maker() as db:
        for i in range(5):
            c = Company(name=f"Co{i}", ats="lever", slug=f"co{i}")
            db.add(c)
            await db.flush()
            db.add(JobLink(user_id=uid, url=f"https://jobs.lever.co/co{i}/1", status="resolved", company_id=c.id))
        await db.commit()
    many = await _list_queries(authed, maker)
    assert len((await authed.get("/api/job-links")).json()) == 6
    assert many == one


async def _schedule_queries(client, maker) -> int:
    with queries(maker) as n:
        r = await client.get("/api/admin/schedule")
    assert r.status_code == 200
    return n[0]


async def test_schedule_queries_do_not_grow_with_users(client, maker, make_account, monkeypatch):
    monkeypatch.setattr(get_settings(), "match_daily_llm_budget", 10)
    await make_account("owner@x.com", admin=True)
    await _schedule_queries(client, maker)  # the first request after login also records last_seen_at
    one = await _schedule_queries(client, maker)
    for i in range(4):
        await make_account(f"user{i}@x.com", login=False)
    many = await _schedule_queries(client, maker)
    assert many == one
    rows = {u["email"]: u for u in (await client.get("/api/admin/schedule")).json()["users"]}
    assert len(rows) == 5 and rows["user0@x.com"]["budget_left"] == 10


async def test_batched_budget_matches_the_per_user_rule(maker, monkeypatch):
    from datetime import UTC, datetime

    from jobfinder.models import Job
    from jobfinder.services.matching.service import UNLIMITED, remaining_budget

    monkeypatch.setattr(get_settings(), "match_daily_llm_budget", 3)
    async with maker() as db:
        capped = User(email="a@x.com", match_budget=5)
        default = User(email="b@x.com")
        off = User(email="c@x.com", match_budget_enabled=False)
        c = Company(name="Acme", ats="lever", slug="acme")
        db.add_all([capped, default, off, c])
        await db.flush()
        for i in range(2):
            j = Job(source="lever", external_id=str(i), company_id=c.id, company_name="Acme", title="Eng",
                    url=f"http://x/{i}", dedupe_hash=f"h{i}")
            db.add(j)
            await db.flush()
            db.add(JobMatch(user_id=capped.id, job_id=j.id, profile_version=1, llm_score=50, confidence=0.9,
                           verdict="strong", scored_at=datetime.now(UTC)))
        await db.flush()
        users = [capped, default, off]
        batched = await admin_service.remaining_budgets(db, users)
        assert batched == {u.id: await remaining_budget(db, u.id) for u in users}
        assert batched[capped.id] == 3 and batched[default.id] == 3 and batched[off.id] == UNLIMITED
