import json
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker

from jobfinder.ingest import runner, service
from jobfinder.ingest.base import JobPosting
from jobfinder.ingest.sources.ashby import parse_ashby
from jobfinder.models import Company, Job, Profile, User

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def maker(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


def posting(i, title="Engineer", source="greenhouse", **kw):
    return JobPosting(source=source, external_id=str(i), company_name="Acme", title=f"{title} {i}",
                      url=f"https://x/{i}", location="Boston", description_text="desc", **kw)


async def make_company(db, slug="acme", ats="greenhouse"):
    c = Company(name="Acme", ats=ats, slug=slug)
    db.add(c)
    await db.commit()
    return c


async def active_ids(db):
    return set((await db.scalars(sa.select(Job.external_id).where(Job.is_active.is_(True)))).all())


# ── seed list ────────────────────────────────────────────────────────────
def test_seed_yaml_is_valid():
    entries = service.load_seed()
    assert len(entries) >= 20
    seen = set()
    for e in entries:
        assert e["ats"] in ("greenhouse", "lever", "ashby"), e
        assert 1 <= e["prestige"] <= 5 and e["size"] in service.VALID_SIZES, e
        assert e["slug"] and e["name"] and (e["ats"], e["slug"]) not in seen
        seen.add((e["ats"], e["slug"]))


async def test_sync_companies_is_idempotent_and_updates(maker):
    async with maker() as db:
        await service.sync_companies(db)
        await service.sync_companies(db)
        n = await db.scalar(sa.select(sa.func.count()).select_from(Company))
        assert n == len(service.load_seed())
        await service.sync_companies(db, [{"name": "Stripe Inc", "ats": "greenhouse", "slug": "stripe", "prestige": 5, "size": "large", "industry": "x"}])
        assert (await db.scalar(sa.select(Company.name).where(Company.slug == "stripe"))) == "Stripe Inc"


async def test_sync_companies_stores_sponsorship_flag(maker):
    async with maker() as db:
        await service.sync_companies(db, [
            {"name": "A", "ats": "greenhouse", "slug": "a", "sponsors_visas": True},
            {"name": "B", "ats": "greenhouse", "slug": "b", "sponsors_visas": False},
            {"name": "C", "ats": "greenhouse", "slug": "c"},
        ])
        flags = dict((await db.execute(sa.select(Company.slug, Company.sponsors_visas))).all())
        assert flags == {"a": True, "b": False, "c": None}


# ── upsert / deactivate ──────────────────────────────────────────────────
async def test_upsert_new_then_update_then_dedupe_within_batch(maker):
    async with maker() as db:
        c = await make_company(db)
        assert await service.upsert_postings(db, [posting(1), posting(2)], c.id) == (2, 0)
        # same two + a repeated one inside the batch + one new
        changed = posting(2)
        changed.title = "Renamed"
        assert await service.upsert_postings(db, [posting(1), changed, changed, posting(3)], c.id) == (1, 2)
        titles = dict((await db.execute(sa.select(Job.external_id, Job.title))).all())
        assert titles["2"] == "Renamed" and len(titles) == 3


async def test_upsert_keeps_stored_description_when_incoming_is_empty(maker):
    async with maker() as db:
        c = await make_company(db)
        full = posting(1, salary_min=100, salary_max=200, salary_currency="USD", workplace_type="remote")
        await service.upsert_postings(db, [full, posting(2)], c.id)
        empty, fresh = posting(1), posting(2)
        empty.description_text, fresh.description_text = "", "new text"
        await service.upsert_postings(db, [empty, fresh], c.id)
        rows = {j.external_id: j for j in await db.scalars(sa.select(Job))}
        assert rows["1"].description_text == "desc" and rows["2"].description_text == "new text"
        assert (rows["1"].salary_min, rows["1"].salary_max, rows["1"].workplace_type) == (100, 200, "remote")


async def test_upsert_preserves_first_seen_and_reactivates(maker):
    async with maker() as db:
        c = await make_company(db)
        await service.upsert_postings(db, [posting(1)], c.id)
        job = (await db.scalars(sa.select(Job))).one()
        first_seen = job.first_seen_at
        await db.execute(sa.update(Job).values(is_active=False))
        await db.commit()
        await service.upsert_postings(db, [posting(1)], c.id)
        await db.refresh(job)
        assert job.is_active is True and job.first_seen_at == first_seen and job.last_seen_at >= first_seen


async def test_deactivate_missing_only_touches_that_company(maker):
    async with maker() as db:
        a, b = await make_company(db, "a"), await make_company(db, "b")
        await service.upsert_postings(db, [posting(1), posting(2), posting(3)], a.id)
        await service.upsert_postings(db, [posting(10)], b.id)
        assert await service.deactivate_missing(db, "greenhouse", a.id, ["1", "3"]) == 1
        assert await active_ids(db) == {"1", "3", "10"}


async def test_nul_characters_are_stripped(maker):
    async with maker() as db:
        c = await make_company(db)
        p = posting(1)
        p.description_text = "bad\x00text"
        await service.upsert_postings(db, [p], c.id)
        assert (await db.scalar(sa.select(Job.description_text))) == "badtext"


async def test_dedupe_hash_matches_across_sources(maker):
    async with maker() as db:
        await service.upsert_postings(db, [posting(1, source="greenhouse"), posting(1, source="jobspy")], None)
        hashes = (await db.scalars(sa.select(Job.dedupe_hash))).all()
        assert len(hashes) == 2 and hashes[0] == hashes[1]


# ── ingest_company ───────────────────────────────────────────────────────
def fake_fetcher(postings=None, exc=None):
    async def fetch(client, slug, name):
        if exc:
            raise exc
        return postings
    return fetch


def status_error(code):
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


async def test_ingest_company_full_cycle(maker, monkeypatch):
    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(i) for i in range(1, 8)]))
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        s = await service.ingest_company(db, client, c)
        assert (s.fetched, s.new, s.updated, s.deactivated, s.error) == (7, 7, 0, 0, None)
        assert c.last_fetched_at and c.last_error is None

        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([posting(i) for i in range(1, 7)] + [posting(99)]))
        s = await service.ingest_company(db, client, c)
        assert (s.new, s.updated, s.deactivated) == (1, 6, 1)
        assert await active_ids(db) == {"1", "2", "3", "4", "5", "6", "99"}


async def test_empty_response_guard_keeps_jobs(maker, monkeypatch):
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        await service.upsert_postings(db, [posting(i) for i in range(1, 9)], c.id)
        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([]))
        s = await service.ingest_company(db, client, c)
        assert s.deactivated == 0 and "empty response" in s.error and len(await active_ids(db)) == 8
        assert "empty response" in c.last_error


async def test_small_board_going_empty_does_deactivate(maker, monkeypatch):
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        await service.upsert_postings(db, [posting(1), posting(2)], c.id)
        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher([]))
        s = await service.ingest_company(db, client, c)
        assert s.deactivated == 2 and s.error is None and await active_ids(db) == set()


async def test_fetch_errors_are_recorded_not_raised(maker, monkeypatch):
    async with maker() as db, httpx.AsyncClient() as client:
        c = await make_company(db)
        await service.upsert_postings(db, [posting(1)], c.id)
        monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", fake_fetcher(exc=status_error(404)))
        s = await service.ingest_company(db, client, c)
        assert "not found" in s.error and c.last_error == s.error
        assert await active_ids(db) == {"1"}  # a failed fetch never deactivates anything


async def test_5xx_is_retried_once(maker, monkeypatch):
    monkeypatch.setattr(service.asyncio, "sleep", lambda s: _noop())
    calls = []

    async def flaky(client, slug, name):
        calls.append(1)
        if len(calls) == 1:
            raise status_error(503)
        return [posting(1)]

    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", flaky)
    async with maker() as db, httpx.AsyncClient() as client:
        s = await service.ingest_company(db, client, await make_company(db))
        assert len(calls) == 2 and s.new == 1 and s.error is None


async def _noop():
    return None


async def test_404_is_not_retried(maker, monkeypatch):
    calls = []

    async def gone(client, slug, name):
        calls.append(1)
        raise status_error(404)

    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", gone)
    async with maker() as db, httpx.AsyncClient() as client:
        await service.ingest_company(db, client, await make_company(db))
        assert len(calls) == 1


async def test_ingest_boards_isolates_failures(maker, monkeypatch):
    async def ok(client, slug, name):
        return [posting(1), posting(2)]

    async def bad(client, slug, name):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", ok)
    monkeypatch.setitem(service.ATS_FETCHERS, "lever", bad)
    monkeypatch.setitem(service.ATS_FETCHERS, "ashby", ok)
    results = await service.ingest_boards(maker)
    by_ats = {}
    for r in results:
        by_ats.setdefault(r.source, []).append(r)
    assert all(r.error for r in by_ats["lever"]) and all(r.error is None for r in by_ats["greenhouse"] + by_ats["ashby"])
    assert len(results) == len(service.load_seed())


async def test_ingest_boards_filters_by_ats_and_slug(maker, monkeypatch):
    async def ok(client, slug, name):
        return [posting(1)]

    for ats in ("greenhouse", "lever", "ashby"):
        monkeypatch.setitem(service.ATS_FETCHERS, ats, ok)
    assert {r.source for r in await service.ingest_boards(maker, ats="lever")} == {"lever"}
    only = await service.ingest_boards(maker, slug="stripe")
    assert [r.company for r in only] == ["Stripe"]


async def test_ashby_fixture_with_duplicates_end_to_end(maker):
    jobs = parse_ashby(json.loads((FIX / "ashby_ramp.json").read_text()), "Ramp")
    assert len(jobs) == 4  # the fixture repeats two postings
    async with maker() as db:
        c = await make_company(db, "ramp", "ashby")
        assert await service.upsert_postings(db, jobs, c.id) == (2, 0)
        sal = await db.scalar(sa.select(Job.salary_max).where(Job.title == "Security Engineer, Cloud"))
        assert sal == 290600


# ── runner / jobspy ──────────────────────────────────────────────────────
async def test_run_ingest_summary(maker, monkeypatch):
    async def ok(client, slug, name):
        return [posting(1)]

    async def bad(client, slug, name):
        raise status_error(404)

    monkeypatch.setitem(service.ATS_FETCHERS, "greenhouse", ok)
    monkeypatch.setitem(service.ATS_FETCHERS, "lever", bad)
    monkeypatch.setitem(service.ATS_FETCHERS, "ashby", ok)
    out = await runner.run_ingest(maker, jobspy=False)
    assert out["companies"] == len(service.load_seed()) and out["new"] > 0 and set(out["errors"]) == {"Palantir", "Spotify"}


async def test_expire_stale_for_jobspy(maker):
    from datetime import UTC, datetime, timedelta

    async with maker() as db:
        await service.upsert_postings(db, [posting(1, source="jobspy"), posting(2, source="jobspy")], None)
        await db.execute(sa.update(Job).where(Job.external_id == "1").values(last_seen_at=datetime.now(UTC) - timedelta(days=30)))
        await db.commit()
        assert await service.expire_stale(db, "jobspy", service.JOBSPY_EXPIRE_AFTER) == 1
        assert await active_ids(db) == {"2"}


def test_parse_jobspy_rows():
    import math
    from datetime import date

    from jobfinder.ingest.sources.jobspy_source import parse_jobspy_rows

    rows = [
        {"id": "li-1", "title": "Backend Engineer", "company": "Acme", "job_url": "https://li/1", "location": "Boston, MA, US",
         "date_posted": date(2026, 9, 30), "job_type": "fulltime", "min_amount": 60.0, "max_amount": 80.0, "interval": "hourly",
         "currency": "USD", "is_remote": True, "description": "Great job"},
        {"id": "in-2", "title": "Senior Dev", "company": "Beta", "job_url": "https://in/2", "location": float("nan"),
         "date_posted": None, "min_amount": math.nan, "max_amount": math.nan, "is_remote": False,
         "description": "Pay $140,000 - $180,000"},
        {"id": "x", "title": "", "job_url": "https://bad"},          # no title: skipped
    ]
    a, b = parse_jobspy_rows(rows)
    assert (a.external_id, a.company_name, a.workplace_type) == ("li-1", "Acme", "remote")
    assert (a.salary_min, a.salary_max) == (124800, 166400) and a.posted_at.date() == date(2026, 9, 30)
    assert b.seniority == "senior" and (b.salary_min, b.salary_max) == (140000, 180000) and b.location == ""


async def test_build_queries_from_profiles(maker):
    from jobfinder.ingest.sources.jobspy_source import JobQuery, build_queries

    async with maker() as db:
        for i, status in enumerate([
            {"target_roles": ["Backend Engineer", "SRE"], "target_locations": ["Boston", "Remote"]},
            {"target_roles": ["Backend Engineer"], "target_locations": ["Boston"]},   # duplicate pair
            {"target_roles": ["Data Engineer"]},                                      # default location
            {},
        ]):
            u = User(email=f"u{i}@x.com", password_hash="x")
            db.add(u)
            await db.flush()
            db.add(Profile(user_id=u.id, status=status, background={}))
        await db.commit()
        qs = await build_queries(db, limit=50)
        assert JobQuery("Backend Engineer", "Boston") in qs and JobQuery("Data Engineer", "United States") in qs
        assert len(qs) == len(set(qs)) == 5
        assert len(await build_queries(db, limit=2)) == 2


async def test_fetch_jobspy_survives_a_failing_query(monkeypatch):
    from jobfinder.ingest.sources import jobspy_source as js

    monkeypatch.setattr(js, "PAUSE_BETWEEN_QUERIES_S", 0)

    def fake_scrape(query, n):
        if query.role == "bad":
            raise RuntimeError("blocked")
        return [{"id": f"li-{query.role}", "title": f"{query.role} dev", "job_url": f"https://li/{query.role}", "company": "C"}]

    monkeypatch.setattr(js, "_scrape", fake_scrape)
    out = await js.fetch_jobspy([js.JobQuery("a", "x"), js.JobQuery("bad", "x"), js.JobQuery("b", "x")])
    assert [p.external_id for p in out] == ["li-a", "li-b"]


def test_safe_http_url():
    from jobfinder.ingest.base import safe_http_url

    assert safe_http_url(" https://boards.example/j/1 ") == "https://boards.example/j/1"
    assert safe_http_url("http://x.io/a?b=1") == "http://x.io/a?b=1"
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.example/x", "/relative", "ftp://x/y", "", None, "https://"):
        assert safe_http_url(bad) == ""


async def test_upsert_drops_non_http_urls(maker):
    async with maker() as db:
        c = await make_company(db)
        p = posting(1)
        p.url = "javascript:alert(document.cookie)"
        await service.upsert_postings(db, [p, posting(2)], c.id)
        urls = {j.external_id: j.url for j in (await db.scalars(sa.select(Job))).all()}
        assert urls == {"1": "", "2": "https://x/2"}
