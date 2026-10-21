import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from jobfinder.core.config import get_settings
from jobfinder.ingest import runner, service
from jobfinder.ingest.sources import usajobs
from jobfinder.models import Job, Profile, User
from jobfinder.models.posting import JobPosting
from tests.fakes import handle, serve

FIX = json.loads((Path(__file__).parent / "fixtures" / "usajobs_search.json").read_text())


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(usajobs, "PAUSE_BETWEEN_REQUESTS_S", 0)


def test_parse_usajobs():
    a, b = usajobs.parse_usajobs(FIX)  # third item has no title: skipped
    assert (a.source, a.external_id, a.title) == ("usajobs", "790000001", "Aerospace Engineer")
    assert a.company_name.startswith("National Aeronautics") and a.url == "https://www.usajobs.gov/job/790000001"
    assert a.location == "Houston, Texas; Cleveland, Ohio" and (a.salary_min, a.salary_max) == (99172, 128341)
    assert a.posted_at.date().isoformat() == "2026-10-01" and a.employment_type == "Full-time"
    assert "spacecraft" in a.description_text and "<b>" not in a.description_text
    assert "Support test campaigns." in a.description_text and "U.S. citizenship is required" in a.description_text
    assert b.url.endswith("/apply") and b.workplace_type == "remote" and b.seniority == "senior"
    assert (b.salary_min, b.salary_max) == (94640, 124800)
    assert b.description_text.count("citizen") == 1  # already says it: no duplicate note


def test_parse_usajobs_empty():
    assert usajobs.parse_usajobs({}) == [] and usajobs.parse_usajobs({"SearchResult": None}) == []


async def test_fetch_noop_without_key(monkeypatch, web):
    monkeypatch.setattr(get_settings(), "usajobs_api_key", "")

    def boom(request):
        raise AssertionError("no request expected")

    handle(web, boom)
    async with httpx.AsyncClient() as c:
        assert await usajobs.fetch_usajobs(c) == []


async def test_fetch_headers_paging_and_failure_isolation(monkeypatch, web):
    s = get_settings()
    monkeypatch.setattr(s, "usajobs_api_key", "KEY")
    monkeypatch.setattr(s, "usajobs_email", "me@example.com")
    monkeypatch.setattr(usajobs, "RESULTS_PER_PAGE", 2)
    seen = []

    def handler(request):
        p = request.url.params
        seen.append((p.get("LocationName"), p["Page"]))
        assert request.headers["Authorization-Key"] == "KEY" and request.headers["User-Agent"] == "me@example.com"
        assert request.headers["Host"] == "data.usajobs.gov" and "0861" in p["JobCategoryCode"]
        if p.get("LocationName") == "Bad":
            return httpx.Response(403)
        items = FIX["SearchResult"]["SearchResultItems"]
        page = int(p["Page"])
        chunk = [items[0], items[1]] if page == 1 else [{**items[0], "MatchedObjectId": "790000009"}]
        return httpx.Response(200, json={"SearchResult": {"SearchResultCountAll": 3, "SearchResultItems": chunk}})

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        out = await usajobs.fetch_usajobs(c, [None, "Bad", "Boston"])
    assert [p.external_id for p in out] == ["790000001", "790000002", "790000009"] * 2
    assert seen == [(None, "1"), (None, "2"), ("Bad", "1"), ("Boston", "1"), ("Boston", "2")]


async def test_fetch_retries_rate_limit(monkeypatch, web):
    monkeypatch.setattr(get_settings(), "usajobs_api_key", "KEY")
    monkeypatch.setattr(get_settings(), "usajobs_email", "me@example.com")
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=FIX)

    handle(web, handler)
    async with httpx.AsyncClient() as c:
        assert len(await usajobs.fetch_usajobs(c)) == 2 and len(calls) == 2


async def test_fetch_raises_when_every_query_fails(monkeypatch, web):
    monkeypatch.setattr(get_settings(), "usajobs_api_key", "BAD")
    monkeypatch.setattr(get_settings(), "usajobs_email", "me@example.com")
    serve(web, status=401)
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.HTTPStatusError):
            await usajobs.fetch_usajobs(c, [None, "Boston"])


async def test_build_locations(maker):
    async with maker() as db:
        for i, st in enumerate([{"target_locations": ["Boston", "Remote", "Seattle"]}, {"target_locations": ["Boston"]}, {}]):
            u = User(email=f"u{i}@x.com", password_hash="x", activated_at=datetime.now(UTC))
            db.add(u)
            await db.flush()
            db.add(Profile(user_id=u.id, status=st, background={}))
        ghost = User(email="ghost@x.com", password_hash="x")  # never activated: ignored
        db.add(ghost)
        await db.flush()
        db.add(Profile(user_id=ghost.id, status={"target_locations": ["Denver", "boston"]}, background={}))
        await db.commit()
        assert await usajobs.build_locations(db) == [None, "Boston", "Seattle"]
        assert await usajobs.build_locations(db, limit=1) == [None, "Boston"]


async def test_ingest_usajobs_upserts_and_expires(maker, monkeypatch, web):
    monkeypatch.setattr(get_settings(), "usajobs_api_key", "KEY")
    monkeypatch.setattr(get_settings(), "usajobs_email", "me@example.com")
    state = {"payload": FIX}
    handle(web, lambda request: httpx.Response(200, json=state["payload"]))
    async with maker() as db:  # a stale job from an earlier run, never seen again
        old = JobPosting(source="usajobs", external_id="old", company_name="X", title="Old", url="https://x/o")
        await service.upsert_postings(db, [old], None)
        await db.execute(sa.update(Job).where(Job.external_id == "old").values(last_seen_at=datetime.now(UTC) - timedelta(days=30)))
        await db.commit()

    st = await runner.ingest_usajobs(maker)
    assert (st.fetched, st.new, st.updated, st.deactivated, st.error) == (2, 2, 0, 1, None)
    st = await runner.ingest_usajobs(maker)
    assert (st.new, st.updated, st.deactivated) == (0, 2, 0)
    async with maker() as db:
        active = set((await db.scalars(sa.select(Job.external_id).where(Job.is_active.is_(True), Job.source == "usajobs"))).all())
        assert active == {"790000001", "790000002"}
        job = await db.scalar(sa.select(Job).where(Job.external_id == "790000001"))
        assert job.salary_min == 99172 and "citizenship" in job.description_text

    state["payload"] = {"SearchResult": {"SearchResultItems": []}}  # empty answer must not expire anything
    st = await runner.ingest_usajobs(maker)
    assert st.fetched == 0 and st.deactivated == 0 and st.error is None


async def test_run_ingest_skips_usajobs_without_key(maker, monkeypatch):
    monkeypatch.setattr(get_settings(), "usajobs_api_key", "")

    async def boom(*a, **k):
        raise AssertionError("must not run")

    monkeypatch.setattr(runner, "ingest_usajobs", boom)
    out = await runner.run_ingest(maker, ats="usajobs", jobspy=False)
    assert out["companies"] == 0


async def test_key_without_email_is_disabled(monkeypatch, web):
    s = get_settings()
    monkeypatch.setattr(s, "usajobs_api_key", "KEY")
    monkeypatch.setattr(s, "usajobs_email", "")
    assert not s.usajobs_enabled

    def boom(request):
        raise AssertionError("no request expected")

    handle(web, boom)
    async with httpx.AsyncClient() as c:
        assert await usajobs.fetch_usajobs(c) == []
